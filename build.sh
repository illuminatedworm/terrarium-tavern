#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
source tools/desktop-env.sh

if [[ ! -f node_modules/vite/bin/vite.js ]]; then
    printf '%s\n' '请先运行 ./setup.sh 安装前端依赖。' >&2
    exit 1
fi
npm run build
printf '%s\n' '构建完成：dist/ 和 src/tavern/static/web/ 已更新。'
