#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

usage() { printf '%s\n' '用法：./stop.sh [--port PORT]'; }
port=8000
while (($#)); do
    case "$1" in
        --port|-p)
            if (($# < 2)); then usage >&2; exit 2; fi
            port=$2; shift 2
            ;;
        -h|--help) usage; exit 0 ;;
        *) printf '未知参数：%s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done
if [[ ! $port =~ ^[0-9]{1,5}$ ]] || ((10#$port < 1 || 10#$port > 65535)); then
    printf '%s\n' '端口必须是 1 到 65535 之间的整数。' >&2
    exit 2
fi
port=$((10#$port))
if [[ ! -x .venv/bin/python ]]; then
    printf '%s\n' '请先运行 ./setup.sh' >&2
    exit 1
fi
.venv/bin/python - "$port" <<'PY'
import json
import os
import signal
import sys
import urllib.request
from pathlib import Path

port = int(sys.argv[1])
base = f"http://127.0.0.1:{port}"
# Local control requests must not be sent through an installation/API proxy.
client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with client.open(f"{base}/api/health", timeout=3) as response:
        health = json.load(response)
except (OSError, ValueError):
    print(f"端口 {port} 上没有可访问的酒馆服务。")
    sys.exit(0)
if not isinstance(health, dict) or health.get("runtime") != "KohakuTerrarium":
    sys.exit("目标不是 Terrarium Tavern，未执行关停。")

request = urllib.request.Request(
    f"{base}/api/shutdown", data=b'{"confirm":true}',
    headers={"Content-Type": "application/json"}, method="POST",
)
try:
    with client.open(request, timeout=5) as response:
        response.read()
except OSError:
    # Older servers have no shutdown callback. Only signal a verified listener.
    inodes = set()
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        if not table.exists():
            continue
        for row in table.read_text().splitlines()[1:]:
            fields = row.split()
            address, local_port = fields[1].split(":")
            loopback = {"0100007F", "00000000", "00000000000000000000000000000000",
                        "0000000000000000FFFF00000100007F"}
            if fields[3] == "0A" and address in loopback and int(local_port, 16) == port:
                inodes.add(fields[9])
    if not inodes:
        print("服务已关闭。")
        sys.exit(0)
    listeners = []
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            if not any(os.readlink(fd) in {f"socket:[{inode}]" for inode in inodes}
                       for fd in (process / "fd").iterdir()):
                continue
            args = (process / "cmdline").read_bytes().decode(errors="replace").split("\0")
            is_tavern = any(args[i:i + 2] == ["-m", "tavern"] for i in range(len(args) - 1))
            is_legacy = any("uvicorn" in arg for arg in args) and any("agentic-tavern" in arg for arg in args)
            if not (is_tavern or is_legacy):
                sys.exit("无法确认服务进程身份，未强制结束。")
            listeners.append(int(process.name))
        except OSError:
            continue  # The process may have exited or belong to another user.
    if not listeners:
        sys.exit("无法确认服务进程身份，未强制结束。")
    for pid in listeners:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    print("旧版本服务已关闭。再次启动后将使用正常关停接口。")
else:
    print("正在关闭服务，未完成的回合将保留草稿。")
PY
