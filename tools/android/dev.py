"""Windows USB development loop: incremental build, isolated install, device probes and logs."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlsplit

from recover_native import DEFAULT_CACHE, ROOT

APP = "com.terrarium.tavern"
DEV_APP = APP + ".dev"
INSTRUMENTATION = DEV_APP + "/com.terrarium.tavern.DevSmokeInstrumentation"


def run(command, *, env=None, log=None, timeout=30):
    # Stream long builds to disk so a stalled command is inspectable before it exits.
    with log.open("w", encoding="utf-8") if log else nullcontext(None) as destination:
        process = subprocess.Popen(
            [str(part) for part in command],
            cwd=ROOT,
            env=env,
            stdout=destination or subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            output, _ = process.communicate(timeout=timeout)
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            # A .bat launcher owns a Java child. Close only this command's process tree.
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
            process.communicate()
            raise
    if log:
        output = log.read_text(encoding="utf-8", errors="replace")
    if process.returncode:
        location = f"；完整输出：{log}" if log else ""
        raise RuntimeError(f"{Path(command[0]).name} 失败（{process.returncode}）{location}\n{output}")
    return output.strip()


class Device:
    def __init__(self, adb, serial):
        self.adb = adb
        rows = re.findall(
            r"^(\S+)\s+(device|offline|unauthorized|recovery|sideload|bootloader)\b",
            run([adb, "devices"]),
            re.MULTILINE,
        )
        if serial:
            rows = [row for row in rows if row[0] == serial]
        if len(rows) != 1 or rows[0][1] != "device":
            raise RuntimeError("需要唯一且已授权的 USB/ADB 设备；多设备时使用 --serial。\n" + str(rows))
        self.serial = rows[0][0]

    def call(self, *args, **kwargs):
        return run([self.adb, "-s", self.serial, *args], **kwargs)

    def describe(self):
        properties = self.call("shell", "getprop")
        values = dict(re.findall(r"^\[([^]]+)\]: \[(.*)\]$", properties, re.MULTILINE))
        memory = self.call("shell", "cat", "/proc/meminfo")
        total = int(re.search(r"^MemTotal:\s+(\d+)", memory, re.MULTILINE)[1]) * 1024
        return {
            "serial": self.serial,
            "model": values["ro.product.model"],
            "sdk": int(values["ro.build.version.sdk"]),
            "abi": values["ro.product.cpu.abilist"],
            "soc": values.get("ro.soc.model", ""),
            "page_bytes": int(self.call("shell", "getconf", "PAGESIZE")),
            "memory_bytes": total,
        }

    def logs(self, package, destination):
        packages = self.call("shell", "cmd", "package", "list", "packages", "-U", package)
        match = re.search(r"^package:" + re.escape(package) + r" uid:(\d+)$", packages, re.MULTILINE)
        if not match:
            raise RuntimeError("设备未安装 " + package)
        # UID includes the host and :realm/:documents; do not clear the shared system log buffer.
        self.call("logcat", "-d", "-v", "threadtime", "--uid=" + match[1], log=destination)


def build_environment(cache):
    environment = os.environ.copy()
    jdk = sorted((cache / "jdk").glob("*/bin/java.exe"))
    if len(jdk) != 1:
        raise RuntimeError(f"需要 {cache / 'jdk'} 下唯一的 JDK 17；构建环境见 docs/development/android.md")
    environment.update(
        JAVA_HOME=str(jdk[0].parents[1]),
        ANDROID_HOME=str(cache / "sdk"),
        GRADLE_USER_HOME=str(cache / "gradle-home"),
        TAVERN_BUILD_PYTHON=str(cache / "build-env/Scripts/python.exe"),
    )
    return environment


def proxy_options(environment):
    options = []
    for scheme in ("http", "https"):
        value = environment.get(scheme.upper() + "_PROXY") or environment.get(scheme + "_proxy")
        if value:
            proxy = urlsplit(value)
            if proxy.scheme != "http" or not proxy.hostname or not proxy.port or proxy.username:
                raise ValueError("Gradle 代理需要不含账号的 http://主机:端口 地址")
            options.extend((f"-D{scheme}.proxyHost={proxy.hostname}", f"-D{scheme}.proxyPort={proxy.port}"))
    return options


def build(cache, output, target, web, online, report):
    environment = build_environment(cache)
    run(
        [
            environment["TAVERN_BUILD_PYTHON"],
            ROOT / "tools/android/prepare_llama.py",
            *([] if online else ["--offline"]),
        ],
        env=environment,
        log=output / "llama-prepare.log",
        timeout=120,
    )
    if web:
        started = time.perf_counter()
        print("构建前端与兼容 SDK…", flush=True)
        run(["npm.cmd", "run", "build"], env=environment, log=output / "web-build.log", timeout=180)
        report["web_seconds"] = round(time.perf_counter() - started, 3)
    elif not (ROOT / "src/tavern/static/web/index.html").is_file():
        raise RuntimeError("缺少前端资源，请加 --web 构建")
    variant = "Dev" if target == "dev" else "Debug"
    command = [
        cache / "gradle-8.13/bin/gradle.bat",
        "--no-daemon",
        "--max-workers=2",
        "--project-dir",
        ROOT / "packaging/android",
        "--project-cache-dir",
        cache / "gradle-project",
        *proxy_options(environment),
        ":app:assemble" + variant,
    ]
    if not online:
        command.append("--offline")
    print("增量构建 " + variant + " APK…", flush=True)
    started = time.perf_counter()
    run(command, env=environment, log=output / "build.log", timeout=300)
    report["build_seconds"] = round(time.perf_counter() - started, 3)
    name = variant.lower()
    apk = cache / f"build/app/outputs/apk/{name}/app-{name}.apk"
    badging = run([cache / "sdk/build-tools/35.0.0/aapt2.exe", "dump", "badging", apk])
    expected = DEV_APP if target == "dev" else APP
    if not badging.startswith("package: name='" + expected + "'"):
        raise RuntimeError("APK 包名与目标不一致，停止安装")
    report["apk"] = str(apk)
    return apk


def smoke(device, output, suite):
    print("执行手机自检（" + suite + "）…", flush=True)
    try:
        result = device.call(
            "shell",
            "am",
            "instrument",
            "-w",
            "-r",
            "-e",
            "suite",
            suite,
            INSTRUMENTATION,
            log=output / "smoke.log",
            timeout=900 if suite.startswith("models") else 240,
        )
        match = re.search(r"^INSTRUMENTATION_RESULT: report=(.+)$", result, re.MULTILINE)
        if not match or "INSTRUMENTATION_CODE: -1" not in result:
            raise RuntimeError("手机自检失败，完整输出：" + str(output / "smoke.log") + "\n" + result)
        report = json.loads(match[1])
        if report.get("ok") is not True:
            raise RuntimeError("手机自检未完成：" + match[1])
        return report
    finally:
        try:
            device.logs(DEV_APP, output / "logcat.txt")
        finally:
            # Instrumentation owns only the dev app; leave no background probe services running.
            device.call("shell", "am", "force-stop", DEV_APP)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("doctor", "build", "deploy", "check", "smoke", "logs"))
    parser.add_argument("--serial", help="ADB 序列号；只连接一台已授权设备时自动选择")
    parser.add_argument("--target", choices=("dev", "app"), default="dev", help="app 明确选择个人使用包")
    parser.add_argument("--web", action="store_true", help="web/compat/vendor 前端内容改变时重建资源")
    parser.add_argument("--online", action="store_true", help="允许 Gradle 下载缺失依赖；默认离线")
    parser.add_argument(
        "--suite",
        choices=(
            "all",
            "native",
            "sessions",
            "scripts",
            "documents",
            "restart",
            "models",
            "ui",
            "devices",
            "models-opencl",
            "models-hexagon",
        ),
        default="all",
    )
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("当前工具使用 Windows Android 构建环境")
    if args.command in {"check", "smoke"} and args.target != "dev":
        parser.error("自动自检只运行独立开发版，不能指向个人数据")
    cache = DEFAULT_CACHE
    output = cache / "dev"
    output.mkdir(parents=True, exist_ok=True)
    report = {"command": args.command, "target": args.target, "ok": False}
    try:
        device = None
        if args.command != "build":
            device = Device(cache / "sdk/platform-tools/adb.exe", args.serial)
            report["device"] = device.describe()
        if args.command in {"build", "deploy", "check"}:
            if device and (
                report["device"]["page_bytes"] != 4096 or "arm64-v8a" not in report["device"]["abi"]
            ):
                raise RuntimeError("当前 APK 需要 ARM64、4 KiB 内存页设备")
            apk = build(cache, output, args.target, args.web, args.online, report)
            if device:
                print("通过 ADB 覆盖安装…", flush=True)
                started = time.perf_counter()
                try:
                    device.call("install", "-r", "-t", apk, log=output / "install.log", timeout=90)
                except subprocess.TimeoutExpired as failure:
                    raise RuntimeError(
                        "ADB 未在 90 秒内收到安装结果，请检查手机系统安装器的确认或验证提示。"
                        "安装状态未知；确认完成后可直接运行 smoke，无需重新构建传包。"
                    ) from failure
                report["install_seconds"] = round(time.perf_counter() - started, 3)
        if args.command in {"check", "smoke"}:
            report["smoke"] = smoke(device, output, args.suite)
        if args.command == "logs":
            device.logs(DEV_APP if args.target == "dev" else APP, output / "logcat.txt")
            report["logcat"] = str(output / "logcat.txt")
        report["ok"] = True
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as failure:
        report["error"] = str(failure)
    finally:
        # Fixed filenames retain the latest run without accumulating logs or story data.
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
