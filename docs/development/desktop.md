# 桌面开发与便携包

桌面开发环境和成品运行环境分别维护，两者共用应用代码、锁文件、界面、数据格式和默认的普通浏览器入口。Android 构建使用独立的 `android-dev/`，不使用桌面运行时。

## 开发环境

运行根目录的 `setup.ps1` 或 `setup.sh`：

- Python 使用标准 CPython 3.13 和 uv 管理的 `.venv`，支持测试、可编辑安装和子进程。新准备的基础解释器放在长期保留的 `desktop-dev/python/`，不属于 `.cache/` 中的临时文件。
- 默认下载固定版本的官方 Node.js 二进制包（含 npm），放在 `desktop-dev/node/`，不修改系统 PATH。`build`、`run-dev` 和 `start` 脚本只在本进程中优先使用该版本，子进程继承同一 PATH。尚未下载内置 Node 时，也可使用 PATH 中的 Node.js 22+。
- 已有系统 Node 时，可用 `setup.ps1 -SkipNodeDownload` 或 `./setup.sh --skip-node-download` 跳过下载；启动脚本仍优先使用已准备好的内置 Node。
- 安装代理沿用调用环境的 `HTTP_PROXY`、`HTTPS_PROXY`，也可使用安装脚本的代理参数。下载地址、版本与 SHA-256 集中在 [`runtimes.json`](../../packaging/desktop/runtimes.json)，不会跟随 latest 自动升级。

内置工具链目前提供 Windows x64 与 Linux x64。其他架构可跳过 Node 下载、使用系统 Node 继续源码开发；能否安装 Python 的各项原生依赖取决于该平台的上游支持。

**迁移旧 `.venv`**：已有 `.venv` 若仍引用 `.cache/python`，安装脚本不会移动或删除它。需要迁移时，先停止开发服务，把原 `.venv` 改名保留，再运行安装脚本创建新环境；确认新环境正常、且旧解释器不再被其他环境引用后，再按实际引用情况清理旧目录。模型权重与应用数据不在虚拟环境中，不需要重新下载。

日常用 `build.ps1` / `build.sh` 构建界面，用 `run-dev.ps1` / `run-dev.sh` 启动开发模式。直接在终端调用 `npm` 时，版本由该终端的 PATH 决定；需要固定版本时使用 `desktop-dev/node/` 中的 npm。

## 系统浏览器

桌面窗口、剧情脚本、制卡 agent 的本地验证和卡片模块打包，都通过 Playwright 使用系统安装的 Microsoft Edge 或 Google Chrome 稳定版。源码安装和便携包都不下载、不分发 Chromium、Headless Shell、FFmpeg 或 WinLDD；浏览器测试也使用同一选择入口。

- `TAVERN_BROWSER=auto`（默认）：Windows 先找 Edge、再找 Chrome；Linux 先找 Chrome、再找 Edge。自动选择只跳过未安装的浏览器，启动失败时不会悄悄换用另一个。
- `TAVERN_BROWSER=msedge` 或 `TAVERN_BROWSER=chrome`：只使用指定浏览器，缺失或启动失败直接报错。
- Windows 查找用户与系统程序目录中的标准安装位置；Linux 查找 `/opt/google/chrome/chrome` 和 `/opt/microsoft/msedge/msedge`。只安装了 WebView2 Runtime 不满足要求。
- 安装脚本和桌面 CLI 在打开数据目录前检查浏览器，输出实际路径和选择值；CLI 备份／恢复不需要浏览器。运行时不调用浏览器安装器。

例如在 PowerShell 中执行 `$env:TAVERN_BROWSER = 'chrome'` 后运行 `start.ps1`，或在 Linux 执行 `TAVERN_BROWSER=msedge ./start.sh`；改回 `auto` 恢复默认顺序。此变量只控制应用自己的窗口与后台脚本，不影响用户打开普通页面时的默认浏览器。Android 使用自己的 WebView 后端，不读取此变量。

**浏览器资料**：受控窗口使用所选数据目录下的 `desktop-profile/`，阅读布局保存在其中；后台 realm 使用独立的临时 context。两者都不读取系统浏览器的个人资料目录。切换浏览器或回退其版本前，可先用设置数据包导出阅读布局，并备份 `desktop-profile/`。浏览器自身的资料格式由浏览器维护，应用不会在启动失败时删除或重置资料；剧情、设置、已提交变量和恢复快照始终以 SQLite 数据为准。

[Playwright 支持系统 Chrome/Edge 稳定版](https://playwright.dev/python/docs/browsers#google-chrome--microsoft-edge)，但浏览器更新和企业策略会影响兼容性；排查时请同时记录启动输出中的路径、浏览器版本和锁定的 Playwright 版本。使用系统浏览器省掉的是下载和磁盘占用；浏览器仍是 Chromium 内核和多进程模型，运行内存不会按包体积同比降低。

## 便携包

| 平台 | Python | 归档 |
| --- | --- | --- |
| Windows x64 | python.org 官方 embeddable ZIP | ZIP |
| Linux x64 | 固定 python-build-standalone GNU/glibc 包 | tar.gz |

- Windows 的 [embeddable Python](https://docs.python.org/3.13/using/windows.html#the-embeddable-package) 不作为开发虚拟环境，也不在其中安装 pip：构建时用开发解释器和 uv 把锁定依赖装到成品的 `Lib/site-packages`，并用 `python313._pth` 声明隔离路径。
- Linux 使用可搬移的独立 Python，保留原有的可执行位和相对符号链接。
- 两端都通过包内解释器的绝对路径启动，使用系统浏览器，不依赖开发机的 Python 注册信息、用户 site-packages 或 Playwright 浏览器缓存。
- Playwright 使用其锁定 wheel 自带的 Node 驱动；成品不另带官方 Node.js、npm 或 Corepack。源码开发仍使用 `desktop-dev/node/` 中含 npm 的官方 Node.js。

在对应系统的项目开发环境中构建；不能在 Windows 上复制原生库拼装 Linux 包，反之亦然：

```powershell
# Windows x64
.\build.ps1
.venv/Scripts/python.exe tools/package_portable.py --target windows-x64
```

```bash
# Linux x64
./build.sh
.venv/bin/python tools/package_portable.py --target linux-x64
```

基础包不含 CUDA 检索依赖；需要时显式加 `--retrieval`，产物文件名带 `-retrieval`，构建会下载较大的 CUDA/PyTorch 依赖。两种包都不含用户模型权重、API 密钥、剧情或开发 `.venv`。`--output` 可指定归档路径，默认输出到 `releases/`。

**构建过程**

- 从 `uv.lock` 冻结导出带哈希的依赖，在新的临时目录安装，并独立构建应用 wheel，不复制当前开发环境。Python 原始下载在使用前检查固定的 SHA-256，校验失败时保留文件并报错。
- 浏览器由目标机器提供，包内清单记录 `browsers.source=system` 和支持的稳定版渠道。构建不启动、不复制浏览器，也不接受旧的 `--browser-cache` 参数；已有的 Playwright 缓存不会被修改或清理。成品不复制个人浏览器资料目录，首次启动也不会补下载浏览器组件。受控窗口、卡片脚本与模块打包共用浏览器选择规则，后台使用所选浏览器的无头模式。
- 只在新安装的临时运行时中，按已核对的包名与路径裁剪：依赖测试、基准文件、Jieba 的 Paddle／词性标注／关键词提取模块和 Jython pickle、Magika 命令行程序、PyMuPDF 的 C/C++ 开发资源。中文词典、HMM 概率代码、运行模块、模型和第三方许可保留，并同步更新受影响的 `.dist-info/RECORD`。开发环境和源码包不裁剪。Office 仍使用 MarkItDown 转换。
- 包内保留运行时与依赖许可、`uv.lock`、依赖清单、运行时来源和逐文件校验清单；归档旁生成 SHA-256。源码包由 `tools/package_release.py` 单独生成。

**日文词典**：桌面依赖不安装 `SudachiDict-core`，`SudachiPy` 固定为 0.6.11。

- 日文词典按需准备固定的 core 20260723。来源、wheel 与编译词典的 SHA-256 和字节数由 `src/tavern/search/japanese_dictionary.py` 锁定，并写入便携包的 `runtime/sources.json`。
- 下载和离线导入只提取／复制编译数据，不执行 wheel 代码或 pip。临时文件经大小和哈希校验后，原子替换 `data/resources/sudachi-core/20260723/system.dic`（自定义数据目录同理）；首次复用时也校验完整文件。
- 同一进程共用一个准备任务，文件锁拒绝其他进程并发写入；中断的文件不算就绪，重试从头准备。取消某次检索不会取消共享下载；资源页的取消按钮和应用退出会取消下载，并清理该任务自己的临时文件。
- 数据库格式不变；词典不属于日志／索引缓存，也不进入剧情备份。Android 的依赖标记保留 `SudachiDict-core==20260723; sys_platform == 'android'`，APK 内置词典及其提取方式不变。

**使用**：Windows 运行 `点击开始.cmd` 或 `start.ps1`，Linux 运行 `./start.sh`，参数和数据目录语义与源码版一致，启动时输出实际解释器路径。更新时解压到新程序目录，保留或恢复所选数据目录，避免混用两版运行时。

## Linux 系统边界

Linux 包面向 Ubuntu 22.04/24.04、Debian 12 x64 等 glibc 发行版，不支持 Alpine/musl。目标机器需安装 Google Chrome 或 Microsoft Edge 稳定版，并满足浏览器和[所锁定 Playwright 的系统要求](https://playwright.dev/python/docs/intro#system-requirements)；NSS、字体、X11 等共享库由浏览器安装包和发行版提供。应用首次启动时不会自动调用系统包管理器。受控桌面窗口还需要可用的图形会话。

## 验证

在目标平台运行 `tools/verify_release.py <归档路径>`。

- 源码包验证会重新安装开发依赖。
- 便携包验证不联网安装，使用解压后的 Python、Playwright 自带的 Node 驱动和系统 Edge/Chrome，检查：包内无浏览器组件、文件哈希、符号链接、应用来源、基础包不含 torch、浏览器执行、依赖 RECORD 一致性、无词典时中英文分词正常且日文明确报错、Office 转换、PDF 读写、隔离数据目录启动、拒绝重复启动、正常关停与备份恢复。
- Windows 验证会从 PATH 中去掉开发用的 Python/Node/uv；Linux 验证使用标准系统命令目录。
- 便携启动、构建和验证都会清除继承的 `PLAYWRIGHT_NODEJS_PATH` 和 `PLAYWRIGHT_BROWSERS_PATH`，确保使用锁定 Playwright 自带的驱动与系统浏览器。隔离的发行验证使用 `auto` 选择，不继承开发环境的 `TAVERN_` 设置。
- 验证目录、报告和日志位于受忽略的 `.cache/` 下，不使用个人数据。

完整构建与验证按 [AGENTS.md](../../AGENTS.md) 的耗时和测试执行纪律运行。当前主机上的验证不代表干净的 Windows 虚拟机、所有 Linux 发行版或 GPU 检索都已验证。
