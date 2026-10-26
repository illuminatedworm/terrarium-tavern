# Terrarium Tavern 便携版

解压到可写的新目录。Windows x64 双击 `点击开始.cmd`；Linux x64 在该目录运行 `./start.sh`。
默认访问 <http://127.0.0.1:51273>，退出时运行 `stop.ps1` 或 `./stop.sh`。

包内附带 Python、锁定的 Python 依赖、已构建界面与完整 Playwright Chromium。
Playwright 使用其自带的 Node 驱动；包内不另带 Node.js 工具链、npm 或 Corepack。
受控窗口与无头任务共用同一套完整 Chromium，不另带 Headless Shell。
依赖测试和已核对的开发资源已精简，中文词典、运行模块与许可保留；Office 仍使用 MarkItDown。
首次启动不运行安装器、不联网下载依赖，不需要系统 Python、Node、uv 或 Git。
连接模型 API、联网查询和用户主动下载的资源仍需要网络。

日文词典独立准备：首次实际使用日文分词时（包括日文世界书建立索引），自动下载固定的
Sudachi core 20260723；纯中英文检索不需要下载。约下载 68.9 MiB，落盘 207.4 MiB，
保存在所选数据目录的 `resources/sudachi-core/20260723/system.dic`，以后复用。
设置 → 通用 → 本地资源可查看进度、取消、重试及提前准备，也可离线导入指定版本的 wheel
或 `system.dic`。下载使用“联网与代理”的本地资源下载代理开关；SHA-256 校验成功后才生效。
准备失败或取消会明确阻止所需的日文检索，不替换分词算法或漏掉世界书条目；完成准备后重试原操作。
离线使用前请提前准备词典；迁移到新数据目录时须另复制该资源目录或重新导入，剧情备份不包含词典。

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

`runtime/sources.json` 记录 Python 的固定来源及校验值；`requirements.txt` 记录锁定依赖，
`release-manifest.json` 记录实际包内文件、相对链接与校验值，归档旁有 SHA-256 文件。
Python 依赖许可保留在各自的 `.dist-info` 中，Python、Playwright 自带 Node 驱动与 Chromium 也保留各自许可。
项目许可和依赖组合的发行未决事项见 `LICENSE/`；生成便携包不代表授权审查完成。
