# 桌面开发与便携包

桌面开发环境和成品运行环境分别维护，应用代码、锁文件、界面、数据格式与默认普通浏览器入口共用。
Android 构建继续使用独立的 `android-dev/`，不消费桌面运行时。

## 开发环境

运行根目录的 `setup.ps1` 或 `setup.sh`：

- Python 使用标准 CPython 3.13 和 uv 管理的 `.venv`，支持测试、可编辑安装和子进程。
  新准备的基础解释器放在长期保留的 `desktop-dev/python/`，不再把它当作 `.cache/` 中的临时文件。
- 默认下载固定版本的官方 Node.js 二进制包（含 npm），放在 `desktop-dev/node/`；不修改系统 PATH。
  `build`、`run-dev` 和 `start` 脚本只在本进程中优先使用该版本，子进程继承同一 PATH。
  尚未下载内置 Node 时仍可使用 PATH 中的 Node.js 22+。
- 已有系统 Node 时，可用 `setup.ps1 -SkipNodeDownload` 或 `./setup.sh --skip-node-download`
  跳过下载。此选项不改变启动脚本对已准备内置 Node 的优先顺序。
- 安装代理沿用调用环境的 `HTTP_PROXY`、`HTTPS_PROXY`，也可使用安装脚本已有的代理参数。
  下载地址、版本与 SHA-256 集中在 [`runtimes.json`](../../packaging/desktop/runtimes.json)，不跟随 latest 自动升级。

内置工具链目前提供 Windows x64 与 Linux x64。其他架构可跳过 Node 下载并使用系统 Node 继续源码开发；
能否安装 Python 的各项原生依赖仍取决于该平台的上游支持。

已有 `.venv` 若仍引用 `.cache/python`，本次不会移动或删除它。需要迁移时先停止开发服务，
把原 `.venv` 改名保留，再运行安装脚本创建新环境；确认新环境正常且旧解释器不再被其他环境引用后，
再按实际引用情况清理旧目录。模型权重与应用数据不属于虚拟环境，不随迁移重新下载。

日常使用 `build.ps1` / `build.sh` 构建界面，`run-dev.ps1` / `run-dev.sh` 启动开发模式。
直接在终端调用 `npm` 时仍由该终端的 PATH 决定版本；需要固定版本时使用 `desktop-dev/node/` 内的 npm。

## 便携包

| 平台 | Python | Node | 归档 |
| --- | --- | --- | --- |
| Windows x64 | python.org 官方 embeddable ZIP | 官方 Windows x64 ZIP，含 npm | ZIP |
| Linux x64 | 固定 python-build-standalone GNU/glibc 包 | 官方 Linux x64 tar.xz，含 npm | tar.gz |

Windows [embeddable Python](https://docs.python.org/3.13/using/windows.html#the-embeddable-package)
不作为开发虚拟环境使用，不在其中安装 pip；构建时用开发解释器和 uv
把锁定依赖安装到成品的 `Lib/site-packages`，用 `python313._pth` 声明隔离路径。
Linux 使用可搬移的独立 Python，保留原有可执行位和相对符号链接。两端均通过包内的绝对解释器路径启动，
并使用包内 Chromium；不依赖开发机的 Python 注册信息、用户 site-packages 或浏览器缓存。

在对应系统的项目开发环境中构建，不能在 Windows 上复制原生库来拼装 Linux 包，反之亦然：

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

基础包不含 CUDA 检索依赖。需要包含时显式增加 `--retrieval`，产物文件名带 `-retrieval`；
此构建会下载较大的 CUDA/PyTorch 依赖。两种包都不包含用户模型权重、API 密钥、剧情或开发 `.venv`。
`--output` 可指定归档文件路径，默认输出到 `releases/`。

构建从 `uv.lock` 冻结导出带哈希的依赖，在新的临时目录安装，并独立构建应用 wheel，
不复制当前开发环境。Python/Node 原始下载在使用前检查固定 SHA-256；校验失败保留文件并报错。
Chromium 版本由锁定的 Playwright 决定，随包携带完整浏览器与无头运行组件。
已有完整的同修订 Playwright 程序缓存时，可显式传入 `--browser-cache <缓存目录>`，
按所锁定 Playwright 声明的组件逐一核对并只读复制，避免重复下载；缺项直接报错。
不会复制个人浏览器资料目录，包内清单记录本次使用下载还是指定缓存。
包内保留运行时与依赖许可、`uv.lock`、依赖清单、运行时来源和逐文件校验清单；归档旁生成 SHA-256。
源码包仍由 `tools/package_release.py` 单独生成。

Windows 使用 `start.cmd` 或 `start.ps1`；Linux 使用 `./start.sh`。参数及数据目录语义与源码版一致。
便携包更新到新程序目录，保留或恢复所选数据目录，避免混用两版运行时。启动会输出实际解释器路径。

## Linux 系统边界

Linux 包面向 Ubuntu 22.04/24.04、Debian 12 x64 等 glibc 发行版，不支持 Alpine/musl。
各系统的浏览器支持范围以[所锁定 Playwright 的系统要求](https://playwright.dev/python/docs/intro#system-requirements)为准。
附带 Python 和 Node 不等于附带完整操作系统：Chromium 仍依赖系统 NSS、字体、X11 等共享库。
若目标系统缺少这些库，可使用包内 Python 执行 `-m playwright install-deps chromium`，按提示提供 sudo 权限。
不在应用首次启动时自动调用系统包管理器。受控桌面窗口还需可用的图形会话；普通浏览器模式保持原行为。

## 验证

在目标平台运行 `tools/verify_release.py <归档路径>`。源码包验证重新安装开发依赖；便携包验证不会联网安装，
只使用解压后的 Python、Node 和 Chromium，检查文件哈希、符号链接、应用来源、基础包无 torch、
浏览器执行、隔离数据目录启动、重复启动拒绝、正常关停与备份恢复。
Windows 验证去掉 PATH 中的开发 Python/Node/uv，Linux 验证使用标准系统命令目录和包内 Node。
验证目录、报告和日志在受忽略的 `.cache/` 下，不使用个人数据。

完整构建与验证仍按 [AGENTS.md](../../AGENTS.md) 的耗时和测试执行纪律运行；
当前主机验证不代表干净 Windows 虚拟机、所有 Linux 发行版或 GPU 检索均已验证。
