"""A conversation's immutable local host and wall-clock snapshot."""
import platform
import sys
import time
from datetime import datetime


def system_version():
    system = platform.system()
    if system == "Windows":
        version = sys.getwindowsversion()
        # Windows 11 still reports NT 10.0; only client builds >= 22000 are 11.
        if version.product_type == 1 and version.major == 10:
            return "Windows 11" if version.build >= 22000 else "Windows 10"
        return "Windows " + (platform.win32_ver()[0] or platform.release())
    if system == "Darwin":
        return "macOS " + (platform.mac_ver()[0] or "版本未知")
    if system == "Linux":
        try:
            release = platform.freedesktop_os_release()
        except OSError:
            return "Linux（发行版未识别）"
        return release.get("PRETTY_NAME") or " ".join(
            part for part in (release.get("NAME", "Linux"), release.get("VERSION_ID")) if part)
    return " ".join(part for part in (system, platform.release()) if part) or "系统未识别"


def capture_environment(timestamp=None):
    local = datetime.fromtimestamp(time.time() if timestamp is None else timestamp).astimezone()
    minutes = int(local.utcoffset().total_seconds() / 60)
    hours, remainder = divmod(abs(minutes), 60)
    offset = f"UTC{'+' if minutes >= 0 else '-'}{hours}"
    if remainder:
        offset += f":{remainder:02d}"
    return {"system": system_version(), "local_time": local.strftime("%Y-%m-%d %H:%M:%S"), "utc_offset": offset}


def environment_message(snapshot):
    content = ("对话开始时的时间与系统环境（固定快照，不随后续消息更新）：\n"
               f"操作系统：{snapshot['system']}\n"
               f"本地时间：{snapshot['local_time']}\n"
               f"时区：{snapshot['utc_offset']}")
    if snapshot.get("legacy"):
        content += "\n旧对话补录：系统版本按补录时的后端运行环境记录；时间按原始首次请求时间戳及补录时的本地时区换算，缺失时标为未记录。"
    return {"role": "system", "message_type": "editor_environment", "runtime_phase": "header",
            "content": content, "_cache_static": True}
