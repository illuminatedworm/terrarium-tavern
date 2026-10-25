# Terrarium Tavern 便携版

解压到可写的新目录。Windows x64 双击 `点击开始.cmd`；Linux x64 在该目录运行 `./start.sh`。
默认访问 <http://127.0.0.1:51273>，退出时运行 `stop.ps1` 或 `./stop.sh`。

包内附带 Python、Node.js（含 npm）、锁定的 Python 依赖、已构建界面与 Playwright Chromium。
首次启动不运行安装器、不联网下载依赖，不需要系统 Python、Node、uv 或 Git。
连接模型 API、联网查询和用户主动下载的资源仍需要网络。

- Windows：Windows x64，使用官方 embeddable Python；Chromium 的系统支持范围以所锁定 Playwright 为准。
- Linux：面向 Ubuntu 22.04/24.04、Debian 12 x64 等 glibc 发行版，不支持 musl/Alpine。
  Chromium 仍需要系统的 NSS、字体、X11 等共享库；缺少时在相应系统上运行
  `runtime/python/bin/python3 -m playwright install-deps chromium`，按提示提供 sudo 权限。
  这是系统依赖准备，不会下载另一套 Python 或 Node。
- 基础包使用 CPU 关键词/BM25 检索。带 `retrieval` 后缀的包另外包含 CUDA 检索依赖，
  使用本地向量与重排仍需 NVIDIA 驱动，并在界面主动下载模型；模型权重不随包分发。

默认保持普通浏览器模式。受控窗口为可选项：Windows 使用 `start.ps1 -Desktop`，Linux 使用
`./start.sh --desktop`。端口和数据目录分别用 `-Port` / `--port`、`-Data` / `--data` 指定。
数据默认存放在 `data/`，程序、运行时与个人数据相互独立。

升级前在应用内导出备份，完成或取消正在生成的回合；解压新版到新程序目录，
再选择原数据目录或恢复备份。不要把新版运行时覆盖到正在运行的旧目录。
旧数据库的兼容性限制同源码版；不兼容格式会在写入前拒绝。

`runtime/sources.json` 记录 Python 与 Node 的固定来源及校验值；`requirements.txt` 记录锁定依赖，
`release-manifest.json` 记录实际包内文件、相对链接与校验值，归档旁有 SHA-256 文件。
Python 依赖许可保留在各自的 `.dist-info` 中，Python/Node/Chromium 也保留各自许可。
项目许可和依赖组合的发行未决事项见 `LICENSE/`；生成便携包不代表授权审查完成。
