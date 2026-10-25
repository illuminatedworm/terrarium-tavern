#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

usage() { printf '%s\n' '用法：./start.sh [--port PORT] [--data DIRECTORY] [--desktop]'; }
port=51273
data=data
window=()
while (($#)); do
    case "$1" in
        --port|-p)
            if (($# < 2)); then usage >&2; exit 2; fi
            port=$2; shift 2
            ;;
        --data|-d)
            if (($# < 2)); then usage >&2; exit 2; fi
            data=$2; shift 2
            ;;
        --desktop) window=(--desktop); shift ;;
        -h|--help) usage; exit 0 ;;
        *) printf '未知参数：%s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done
if [[ ! $port =~ ^[0-9]{1,5}$ ]] || ((10#$port < 1 || 10#$port > 65535)); then
    printf '%s\n' '端口必须是 1 到 65535 之间的整数。' >&2
    exit 2
fi
port=$((10#$port))
if [[ -z $data ]]; then
    printf '%s\n' '数据目录不能为空。' >&2
    exit 2
fi
source tools/desktop-env.sh
if ! $tavern_portable && [[ ! -f dist/index.html && ! -f src/tavern/static/web/index.html ]]; then
    printf '%s\n' '请先运行 ./build.sh' >&2
    exit 1
fi
"$tavern_python" - "$port" <<'PY'
import socket
import sys

port = int(sys.argv[1])
try:
    with socket.create_connection(("127.0.0.1", port), timeout=1):
        pass
except OSError:
    pass  # Uvicorn still detects a listener appearing after this check.
else:
    sys.exit(f"TAVERN_PORT_IN_USE: 端口 {port} 已被使用；请先运行 ./stop.sh，或使用 --port 选择另一端口。")
PY
# bash before 4.4 treats an empty "${window[@]}" as unbound under set -u.
printf 'Python: %s\n' "$tavern_python"
exec "$tavern_python" -m tavern --port "$port" --data "$data" ${window[@]+"${window[@]}"}
