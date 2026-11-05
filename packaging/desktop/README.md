# Terrarium Tavern 便携版

## 开始使用

1. 安装 Microsoft Edge 或 Google Chrome 稳定版（仅有 WebView2 Runtime 不够）。
2. 解压到一个可写的新目录。
3. Windows x64 双击 `点击开始.cmd`；Linux x64 在该目录运行 `./start.sh`。
4. 访问 <http://127.0.0.1:51273>。退出时运行 `stop.ps1` 或 `./stop.sh`。

- 端口和数据目录分别用 `-Port` / `--port`、`-Data` / `--data` 指定。数据默认存放在 `data/`；程序、运行时与个人数据相互独立。
- 默认使用普通浏览器模式。受控窗口是可选项：Windows 用 `start.ps1 -Desktop`，Linux 用 `./start.sh --desktop`。

## 包内有什么

- 已附带 Python、锁定的 Python 依赖和构建好的界面。首次启动不运行安装器、不联网下载依赖，也不需要系统 Python、Node、uv 或 Git。连接模型 API、联网查询和你主动下载的资源仍需要网络。
- Playwright 使用其自带的 Node 驱动，包内不另带 Node.js 工具链、npm 或 Corepack。受控窗口与后台卡片脚本使用系统浏览器，包内不带 Chromium、Headless Shell、FFmpeg 或 WinLDD。
- 依赖测试和已核对的开发资源已精简，中文词典、运行模块与许可保留；Office 仍使用 MarkItDown。
- 基础包使用 CPU 上的关键词/BM25 检索。带 `retrieval` 后缀的包另含 CUDA 检索依赖；使用本地向量与重排仍需要 NVIDIA 驱动，并需在界面中主动下载模型（模型权重不随包分发）。

## 平台

- **Windows**：Windows x64，使用官方 embeddable Python；默认优先使用系统 Edge，其次 Chrome。
- **Linux**：面向 Ubuntu 22.04/24.04、Debian 12 x64 等 glibc 发行版，不支持 musl/Alpine。默认优先 Chrome，其次 Edge；请用浏览器官方安装包准备稳定版，以及 NSS、字体、X11 等系统依赖。

## 选择浏览器

启动时会输出实际使用的浏览器路径。环境变量 `TAVERN_BROWSER` 接受 `auto`（默认）、`msedge`、`chrome`，例如 PowerShell 中执行 `$env:TAVERN_BROWSER = 'chrome'` 后启动，或在 Linux 执行 `TAVERN_BROWSER=msedge ./start.sh`。

- Windows 查找用户／系统程序目录下的标准安装；Linux 查找 `/opt/google/chrome/chrome`、`/opt/microsoft/msedge/msedge`。
- 明确指定的浏览器缺失或启动失败时直接报错，不会自动改用其他浏览器。
- 这一选择不影响普通页面的默认浏览器；应用也不读取系统浏览器的个人资料。受控窗口使用 `data/desktop-profile/`（自定义数据目录同理），切换浏览器或回退版本前，应先导出阅读布局并备份该目录。

## 日文词典

日文词典需单独准备。首次实际进行日文分词时（包括为日文世界书建立索引），会自动下载固定的 Sudachi core 20260723；纯中英文检索不需要下载。

- 约下载 68.9 MiB，占用磁盘 207.4 MiB，保存在所选数据目录的 `resources/sudachi-core/20260723/system.dic`，之后复用。
- “设置 → 通用 → 本地资源”可查看进度、取消、重试和提前准备，也可离线导入指定版本的 wheel 或 `system.dic`。下载使用“联网与代理”中的本地资源下载代理开关；SHA-256 校验成功后才生效。
- 准备失败或取消时，需要日文分词的检索会明确报错，而不是换用其他分词或漏掉世界书条目；准备完成后重试原操作即可。
- 离线使用前请提前准备。迁移到新数据目录时，需另外复制该资源目录或重新导入；剧情备份不含词典。

## 升级

1. 在应用内导出备份，并完成或取消正在生成的回合。
2. 把新版解压到新的程序目录（不要覆盖正在运行的旧目录）。
3. 启动时选择原数据目录，或恢复备份。

旧数据库的兼容性限制与源码版相同；不兼容的格式会在写入前被拒绝。

## 来源与许可

- `runtime/sources.json` 记录 Python 的固定来源和校验值；`requirements.txt` 记录锁定依赖；`release-manifest.json` 记录包内实际文件、相对链接和校验值；归档旁有 SHA-256 文件。
- Python 依赖的许可保留在各自的 `.dist-info` 中，Python 与 Playwright 自带的 Node 驱动也保留各自的许可。
- 项目许可和依赖组合的发行未决事项见 `LICENSE/`；生成便携包不代表授权审查已完成。
