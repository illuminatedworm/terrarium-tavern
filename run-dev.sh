#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

desktop=false
if (($#)); then
    if [[ $# == 1 && ( $1 == --help || $1 == -h ) ]]; then
        printf '%s\n' '用法：./run-dev.sh [--desktop]（后端 8000，Vite 5173）'
        exit 0
    fi
    if [[ $# != 1 || $1 != --desktop ]]; then
        printf '%s\n' '开发模式使用固定端口 8000 和 5173，只接受 --desktop。' >&2
        exit 2
    fi
    desktop=true
fi
source tools/desktop-env.sh
if [[ ! -f node_modules/vite/bin/vite.js ]]; then
    printf '%s\n' '请先运行 ./setup.sh' >&2
    exit 1
fi
# Opt-in owned window: it starts and stops Vite and the backend itself.
if $desktop; then exec "$tavern_python" -m tavern --dev; fi

backend_pid=''
frontend_pid=''
backend_ready=false
cleanup() {
    local status=$?
    trap - EXIT INT TERM HUP
    if [[ -n $frontend_pid ]]; then
        kill -TERM "$frontend_pid" 2>/dev/null || true
        wait "$frontend_pid" 2>/dev/null || true
    fi
    if [[ -n $backend_pid ]]; then
        if kill -0 "$backend_pid" 2>/dev/null; then
            if $backend_ready; then
                bash ./stop.sh --port 8000 || printf '%s\n' '关停接口调用失败，将终止本次启动的后端。' >&2
            fi
            kill -TERM "$backend_pid" 2>/dev/null || true
        fi
        wait "$backend_pid" 2>/dev/null || true
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

port_in_use=$("$tavern_python" - <<'PY'
import socket

try:
    with socket.create_connection(("127.0.0.1", 8000), timeout=1):
        pass
except OSError:
    print("false")
else:
    print("true")
PY
)
backend_healthy() {
    "$tavern_python" - <<'PY'
import json
import sys
import urllib.request

client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with client.open("http://127.0.0.1:8000/api/health", timeout=1) as response:
        health = json.load(response)
    sys.exit(0 if isinstance(health, dict) and health.get("runtime") == "KohakuTerrarium" else 1)
except (OSError, ValueError):
    sys.exit(1)
PY
}

if [[ $port_in_use == true ]]; then
    if ! backend_healthy; then
        printf '%s\n' '端口 8000 已被占用，但无法确认是 Terrarium Tavern 后端。' >&2
        exit 1
    fi
    printf '%s\n' '复用已运行的后端（端口 8000）。'
else
    "$tavern_python" -m tavern --port 8000 --data data &
    backend_pid=$!
    for ((attempt = 0; attempt < 120; attempt++)); do
        if ! kill -0 "$backend_pid" 2>/dev/null; then
            backend_status=0
            wait "$backend_pid" || backend_status=$?
            backend_pid=''
            printf '后端启动失败（退出码 %s）。\n' "$backend_status" >&2
            exit 1
        fi
        if backend_healthy; then backend_ready=true; break; fi
        sleep 0.25
    done
    if ! $backend_ready; then
        printf '%s\n' '等待后端启动超时。' >&2
        exit 1
    fi
    printf '%s\n' '后端已启动（端口 8000）。'
fi

printf '%s\n' '开发页面：http://127.0.0.1:5173'
node node_modules/vite/bin/vite.js --host 127.0.0.1 --strictPort <&0 &
frontend_pid=$!
frontend_status=0
wait "$frontend_pid" || frontend_status=$?
frontend_pid=''
if ((frontend_status)); then
    printf 'Vite 已退出（退出码 %s）。\n' "$frontend_status" >&2
fi
exit "$frontend_status"
