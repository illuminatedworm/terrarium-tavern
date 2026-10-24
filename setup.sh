#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

usage() {
    printf '%s\n' '用法：./setup.sh [--proxy URL] [--local-retrieval]' \
        '默认沿用环境中的代理；--proxy "" 禁用安装代理。'
}

proxy=''
proxy_set=false
local_retrieval=false
while (($#)); do
    case "$1" in
        --proxy)
            if (($# < 2)); then usage >&2; exit 2; fi
            proxy=$2
            proxy_set=true
            shift 2
            ;;
        --local-retrieval) local_retrieval=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) printf '未知参数：%s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

for tavern_command in npm node git; do
    if ! command -v "$tavern_command" >/dev/null 2>&1; then
        printf '缺少 %s，请先安装 Node.js 22+ 和 Git。\n' "$tavern_command" >&2
        exit 1
    fi
done
tavern_node_major=$(node -p 'process.versions.node.split(".")[0]')
if ((tavern_node_major < 22)); then
    printf '%s\n' '需要 Node.js 22 或更新版本。' >&2
    exit 1
fi

if $proxy_set; then
    case "$proxy" in
        ''|http://?*|https://?*) ;;
        *) printf '%s\n' '--proxy 需要完整 HTTP(S) 代理地址，或空字符串。' >&2; exit 2 ;;
    esac
    export HTTP_PROXY="$proxy" HTTPS_PROXY="$proxy" http_proxy="$proxy" https_proxy="$proxy"
    unset ALL_PROXY all_proxy
fi
export UV_PYTHON_INSTALL_DIR="$PWD/.cache/python"

if command -v uv >/dev/null 2>&1; then
    tavern_uv=(uv)
else
    if command -v python3 >/dev/null 2>&1; then
        tavern_python=python3
    elif command -v python >/dev/null 2>&1; then
        tavern_python=python
    else
        printf '%s\n' '缺少 Python 或 uv，请先安装 Python 3（含 venv）或 uv。' >&2
        exit 1
    fi
    if "$tavern_python" -m uv --version >/dev/null 2>&1; then
        tavern_uv=("$tavern_python" -m uv)
    else
        # Keep the bootstrap isolated from distribution-managed Python (PEP 668).
        "$tavern_python" -m venv .cache/uv
        .cache/uv/bin/python -m pip install uv
        tavern_uv=("$PWD/.cache/uv/bin/python" -m uv)
    fi
fi

tavern_sync=(sync --frozen)
if $local_retrieval; then tavern_sync+=(--extra retrieval); fi
"${tavern_uv[@]}" "${tavern_sync[@]}"
tavern_npm=(ci --registry=https://registry.npmjs.org)
if $proxy_set; then tavern_npm+=("--proxy=$proxy" "--https-proxy=$proxy"); fi
npm "${tavern_npm[@]}"
npm run build
.venv/bin/python -m playwright install chromium
printf '%s\n' '安装完成。运行 ./start.sh，然后访问 http://127.0.0.1:8000'
