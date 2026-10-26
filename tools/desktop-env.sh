#!/usr/bin/env bash
# Sourced from the project root; no global PATH or Python installation is changed.
if [[ -x runtime/python/bin/python3 ]]; then
    tavern_portable=true
    tavern_python="$PWD/runtime/python/bin/python3"
    export PLAYWRIGHT_BROWSERS_PATH="$PWD/runtime/browsers" PYTHONNOUSERSITE=1
    unset PYTHONHOME PYTHONPATH PLAYWRIGHT_NODEJS_PATH
else
    tavern_portable=false
    tavern_python="$PWD/.venv/bin/python"
    if [[ ! -x $tavern_python ]]; then
        printf '%s\n' '请先运行 ./setup.sh，或使用完整的便携发行包。' >&2
        return 1
    fi
    tavern_node_prefix=$("$tavern_python" -c 'import json; print(json.load(open("packaging/desktop/runtimes.json"))["node"]["linux-x64"]["prefix"])')
    tavern_node_directory="$PWD/desktop-dev/node/$tavern_node_prefix/bin"
    if [[ -x $tavern_node_directory/node ]]; then
        export PATH="$tavern_node_directory:$PATH"
    fi
fi
