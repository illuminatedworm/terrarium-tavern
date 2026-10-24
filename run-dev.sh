#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if (($#)); then
    if [[ $# == 1 && ( $1 == --help || $1 == -h ) ]]; then
        printf '%s\n' '用法：./run-dev.sh（受控窗口、后端 8000、Vite 5173）'
        exit 0
    fi
    printf '%s\n' '开发模式使用固定端口 8000 和 5173，不接受参数。' >&2
    exit 2
fi
if [[ ! -x .venv/bin/python ]]; then
    printf '%s\n' '请先运行 ./setup.sh' >&2
    exit 1
fi
exec .venv/bin/python -m tavern --dev
