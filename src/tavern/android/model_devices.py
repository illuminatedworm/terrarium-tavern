"""Probe the optional engine under the app UID, without loading weights."""

import asyncio
import logging
import re
from pathlib import Path

ACCEL_EXECUTABLE = "libtavern_accel_server.so"
CONTEXT_TOKENS = 4096
CPU_THREADS = 4
# With b11829 on Adreno 750, a full 512-token fragment plus EOS leaves
# a one-token microbatch whose Q8_0 vectors vary across identical requests.
# OpenCL processes the whole bounded context together; no input is truncated.
MICROBATCH_TOKENS = {"cpu": 512, "opencl:0": CONTEXT_TOKENS, "hexagon:0": 512}
DEVICES = {
    "cpu": ("CPU", "CPU"),
    "opencl:0": ("GPUOpenCL", "OpenCL GPU"),
    "hexagon:0": ("HTP0", "Hexagon NPU（实验性）"),
}
LOGGER = logging.getLogger(__name__)


def native_failure(log):
    """Keep native paths and opaque IDs out of errors that can reach tool callers."""
    missing = re.search(r'library "([^"/]+)" not found', log)
    if missing:
        return "无法加载厂商库 " + missing[1]
    if "cannot locate symbol" in log:
        return "系统或厂商驱动符号与固定引擎不兼容"
    if "remote_handle64_open" in log or "fastrpc" in log.lower():
        return "Hexagon FastRPC 初始化失败，请查看原生日志中的驱动错误"
    return "原生引擎未成功启动，请查看运行日志"


def launch(command, native_directory, callback):
    from java import jarray, jclass

    return jclass("com.terrarium.tavern.LocalModelProcess").start(
        jarray(jclass("java.lang.String"))(command),
        str(native_directory),
        callback,
    )


def device_table(reason="", *, retry=False):
    """CPU is always available; `retry` marks accelerator failures a later warmup may re-probe."""
    return {
        key: {
            "device": key,
            "label": label,
            "available": key == "cpu",
            "reason": "" if key == "cpu" else reason,
            "retry": retry and key != "cpu",
        }
        for key, (_, label) in DEVICES.items()
    }


async def probe(native_directory, minimum_sdk):
    from java import jclass

    result = device_table()
    reason = ""
    if int(jclass("android.os.Build$VERSION").SDK_INT) < minimum_sdk:
        reason = f"固定加速引擎需要 Android API {minimum_sdk} 或更高版本"
    elif not (native_directory / ACCEL_EXECUTABLE).is_file():
        reason = "APK 中缺少 Snapdragon 加速引擎"
    else:
        # The official build uses armv8.7a+fp16+dotprod+i8mm. Check exposed
        # extensions first; the isolated child also catches loader/SIGILL failures.
        required = {"fphp", "asimdhp", "asimddp", "i8mm"}
        try:
            features = re.findall(r"^Features\s*:\s*(.*)$", Path("/proc/cpuinfo").read_text(), re.MULTILINE)
        except OSError:
            reason = "系统未允许读取 CPU 指令信息，无法确认加速引擎兼容性"
        else:
            if not features or any(not required.issubset(set(row.split())) for row in features):
                reason = "CPU 未声明加速引擎所需的 FP16、DotProd 与 I8MM 指令"
    if reason:
        for key in ("opencl:0", "hexagon:0"):
            result[key]["reason"] = reason
        return result

    loop = asyncio.get_running_loop()
    exited = loop.create_future()

    def complete(_):
        if not exited.done():
            exited.set_result(None)

    process = None
    try:
        process = launch(
            [str(native_directory / ACCEL_EXECUTABLE), "--list-devices"],
            native_directory,
            lambda address: loop.call_soon_threadsafe(complete, address),
        )
        await asyncio.wait_for(exited, 20)
        code = await asyncio.to_thread(process.waitForExit, 1)
        log = str(process.getLogTail())
        if code != 0:
            LOGGER.error("Android device probe exited %s:\n%s", code, log)
        for key in ("opencl:0", "hexagon:0"):
            name = DEVICES[key][0]
            match = re.search(r"^\s+" + re.escape(name) + r": (.+)$", log, re.MULTILINE)
            result[key].update(
                available=code == 0 and match is not None,
                retry=code != 0,
                description=match[1] if match else "",
                reason=""
                if code == 0 and match
                else (
                    f"应用内设备列表不含 {name}，请检查系统与厂商驱动兼容性"
                    if code == 0
                    else f"应用内设备探测失败（退出码 {code}）：{native_failure(log)}"
                ),
            )
    except TimeoutError:
        # Cold vendor drivers can exceed the bound while the phone is busy.
        result = device_table("应用内加速设备探测超过 20 秒，预热时将重新探测", retry=True)
    except Exception as error:
        # Optional native libraries can raise Java loader/ProcessBuilder exceptions.
        # Surface that boundary failure as unavailable; never disable the CPU engine.
        LOGGER.error("Android device probe failed: %s", error)
        result = device_table("应用内设备探测失败：" + native_failure(str(error)), retry=True)
    finally:
        if process is not None:
            await asyncio.to_thread(process.close)
    return result


def execution_metrics(device, log):
    if device == "cpu":
        return {"device": f"Android CPU（{CPU_THREADS} 线程）", "actual_device": "cpu", "offloaded_layers": 0}
    name, label = DEVICES[device]
    used = re.search(r"using device " + re.escape(name) + r" \((.*)\) \([^)]*\) -", log)
    layers = re.search(r"offloaded (\d+)/(\d+) layers", log)
    buffer_name = "OpenCL" if device == "opencl:0" else name
    buffer = re.search(re.escape(buffer_name) + r"\s+model buffer size =\s*([\d.]+) MiB", log)
    if not used or not layers or int(layers[1]) == 0 or not buffer or float(buffer[1]) == 0:
        raise RuntimeError(f"{label} 未确认模型层卸载，已停止模型；不会改用 CPU")
    return {
        "device": f"{label} · {used[1]}",
        "actual_device": device,
        "offloaded_layers": int(layers[1]),
        "total_layers": int(layers[2]),
        "model_buffer_mib": float(buffer[1]),
        "execution_note": "模型层已卸载；不受后端支持的算子仍由 CPU 执行。",
    }
