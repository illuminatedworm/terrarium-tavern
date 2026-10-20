"""Disposable Office conversion process; stdin/stdout carry a single UTF-8 JSON exchange."""

import contextlib
import json
import sys

from tavern.character.editor_files import convert_document


def main() -> None:
    try:
        request = json.loads(sys.stdin.buffer.read())
        # Library diagnostics must not corrupt the JSON protocol.
        with contextlib.redirect_stdout(sys.stderr):
            result = convert_document(**request)
    except ValueError as error:
        result = {"error": str(error)}
    except ImportError:
        result = {"error": "Office 解析依赖不可用，请按项目锁文件安装依赖。"}
    except Exception:
        result = {"error": "Office 文件解析失败，请检查文件是否损坏、加密或使用了不支持的格式。"}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    main()
