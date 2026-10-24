# 桌面开发工具链

此目录长期保留开发用 Python、Node.js 和已校验的运行时下载，不随 `.cache/` 清理。
只有本说明纳入 Git，其余内容被忽略；应用数据仍位于所选数据目录。

- `python/`：uv 管理的标准 Python，作为开发 `.venv` 的基础解释器。
- `node/`：按固定版本、平台和架构解压的官方 Node.js 二进制包，含 npm。
- `downloads/`：便携包和开发环境共用的、带固定 SHA-256 的下载文件。

运行 `setup.ps1` 或 `setup.sh` 准备开发环境；可选择系统 Node。
便携发行包使用独立的 `runtime/`，Windows 为 embeddable Python，Linux 为可搬移的独立 Python。
详见[桌面开发与便携包](../docs/development/desktop.md)。
