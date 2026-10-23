# Android APK 构建

Android 版把 Python/FastAPI 后端、Vue 前端和脚本 WebView 装入同一应用，故事数据保存在手机应用私有目录。它不需要连接电脑上的后端。当前产物用于私人安装验证，使用 debug 签名；尚未完成真机验收，也不是公开正式发行。

## 设备范围

- Android 9（API 28）及以上，`arm64-v8a`。
- **当前仅支持 4 KiB 内存页设备；不支持 16 KiB 内存页设备。** libfreetype、libxml2、libexslt、libxslt、jiter、kohakuvault、rpds 仍含 4 KiB 的 ELF 加载段对齐。三个自行编译的扩展已使用 16 KiB 链接选项，这不代表整个 APK 已支持 16 KiB。后续需重新构建这些依赖并验证完整 APK。
- 不提供 CUDA 本地向量模型和重排模型。已有配置保留，界面显示不可用；关键词与 BM25 继续使用原有 Jieba、Sudachi 和 core 词典，没有替换分词或 token 计数方法。
- 主界面和独立脚本进程使用系统 WebView；Office 文档进程只使用内嵌 Python。安装成功不等于脚本、软键盘、后台恢复和长对话已完成真机验证。

可通过 `adb shell getconf PAGESIZE` 查看已连接设备的内存页大小。设备 ABI 可用 `adb shell getprop ro.product.cpu.abi` 查看。

## 构建环境

当前工具面向 Windows 本地构建。先准备以下工具，脚本不会安装或修改系统工具链：

| 工具 | 固定要求 |
|---|---|
| Python | 3.13；项目版本见 `.python-version` |
| JDK | 17 |
| Android SDK | Platform 35、Build Tools 35.0.0 |
| Android NDK | `27.2.12479018` |
| Rust | `1.99.1`，已安装 `aarch64-linux-android` target |
| Gradle | 8.13 |
| Node/npm、uv | 用于已有前端构建与 `uv.lock` 导出 |

默认缓存根为 `.cache/android/`。NDK 默认位于 `.cache/android/sdk/ndk/27.2.12479018`；可给准备工具传 `--ndk` 指向其他安装位置。Rust 默认使用 `.cache/android/cargo` 和 `.cache/android/rustup`，也尊重调用环境中的 `CARGO_HOME`、`RUSTUP_HOME`。

从项目根目录创建专用构建环境：

```powershell
uv venv --python 3.13 .cache/android/build-env
uv pip install --python .cache/android/build-env/Scripts/python.exe --index-url https://pypi.org/simple pip==26.2.2 maturin==1.15.0 setuptools-rust==1.13.0 setuptools==84.2.1 wheel==0.48.0 packaging==26.3
```

构建工具读取标准 `HTTP_PROXY`、`HTTPS_PROXY` 环境变量。需要代理时在当前终端设置；脚本不写入代理地址，不改变全局 pip、uv 或系统代理配置。Python 包下载显式使用 `https://pypi.org/simple`。

## 准备依赖

```powershell
.cache/android/build-env/Scripts/python.exe tools/android/prepare.py --prefer-pypi-native
```

该命令依次恢复固定依赖、编译三个扩展、执行 `uv export --frozen --no-dev --no-emit-project --no-hashes`，按 Android 平台裁剪清单，构建 Jieba 与固定 KohakuTerrarium 源码 wheel，再下载其余 wheel。下载使用 `--only-binary`、`--no-deps`、`android_24_arm64_v8a`、Python 3.13 和 `cp313` ABI。wheel 标签中的 API 24 是扩展的构建基线；应用自身的最低版本仍是 API 28。

构建产物保存在 `.cache/android/wheels/`。原始下载先写临时文件，校验后发布；sdist 按 `uv.lock` 的 SHA-256 验证，普通 PyPI wheel 按锁文件或显式固定版本的官方 PyPI 摘要验证。恢复、编译和下载步骤分别保存来源记录。最终 `.cache/android/requirements.txt` 包含 `--no-index`、`--require-hashes` 与每个 wheel 的固定版本、哈希，Gradle 只消费本地 wheelhouse。

已存在且记录匹配的产物直接校验后复用。首次迁入此前已独立验证的本地编译 wheel 时，可明确传入 `--reuse-existing-builds`；这只核对元数据与 wheel RECORD，并在来源记录中注明没有重新编译。未知来源、哈希冲突或未声明的源码修改会报错，不自动删除缓存或换用其他版本。

各阶段也可单独执行：

```powershell
.cache/android/build-env/Scripts/python.exe tools/android/recover_native.py --skip-pypi-native
.cache/android/build-env/Scripts/python.exe tools/android/build_wheels.py --jobs 4
.cache/android/build-env/Scripts/python.exe tools/android/prepare.py --receipts-only
```

`--receipts-only` 不下载或编译，需要 wheelhouse、精确依赖清单及已缓存的 Sudachi 许可证齐备。它用于已有产物的来源核对、离线哈希锁定和许可资产生成，不替代缺失的上游下载哈希证据；无法从当前锁文件核验的覆盖版本会在记录中明确注明。

## 固定来源与适配

应用使用的 KohakuTerrarium **始终是 2.1.5**，源码固定在 [`e4fb517939ba07086481251717e888cb74dc19a4`](https://github.com/Kohaku-Lab/KohakuTerrarium/tree/e4fb517939ba07086481251717e888cb74dc19a4)。依赖恢复用到的另一个 APK 不会升级这份源码。

原生依赖的备用来源固定为 [KohakuTerrarium 2.1.7 Android APK](https://github.com/Kohaku-Lab/KohakuTerrarium/releases/download/v2.1.7/kohakuterrarium-2.1.7-android.apk)，SHA-256 为 `1234727d90444b841e947ddd0a6970d96b57cdc36b97b20e1073b79516996835`。恢复器只提取列出的依赖根目录和其发行元数据，保留许可证、Python bytecode 与 `.so` 原内容，重建 WHEEL/RECORD 并添加来源说明。它不追踪 `latest`，也不修改 ELF 以伪装页面大小兼容性。

默认可从该 APK 恢复 20 项依赖；`--prefer-pypi-native` 明确改用 PyPI 的 MarkupSafe 3.0.4 与 KohakuVault 0.8.6 Android wheel，其余恢复项仍使用固定 APK。原生与本地编译包不进入普通 pip 下载请求，避免同名同版本的远程 wheel 与本地恢复产物竞争。

三个自行编译的包为 tiktoken 0.14.0、SudachiPy 0.6.11、pydantic-core 2.41.5，源码取自 `uv.lock` 的已校验 sdist。交叉编译使用 [Chaquopy Python 3.13.7 sysroot](https://repo.maven.apache.org/maven2/com/chaquo/python/python/3.13.7/python-3.13.7-aarch64-linux-android.tar.gz)，SHA-256 为 `0d889ce4237b0e37f3c8b45e6094aec693bb5faaf5cbdfb7167e16fedbc1a40e`。

- tiktoken 改用 maturin，显式开启 `python` feature、指定 `tiktoken._tiktoken`，并用 `python-packages` 同时打包 `tiktoken` 与 `tiktoken_ext`。构建时检查必需的插件模块已进入 wheel；只需重建它时可运行 `build_wheels.py --package tiktoken`。
- SudachiPy 改用 maturin；`sudachi-lib/src/plugin/loader.rs` 的 Linux/FreeBSD `.so` 命名条件增加 Android。脚本只接受已核对的原文或精确的补丁结果，遇到其他修改即停下。
- tiktoken 的 sdist 没有 Cargo.lock，Sudachi 的 sdist 带有上游整个 workspace 的锁文件；`tools/android/native-locks/` 保存此次 Python 扩展目标使用的固定 Rust 依赖。pydantic-core 沿用 sdist 自带锁文件。编译统一使用 `--locked`。
- maturin 的解释器参数使用运行脚本的 `sys.executable`，不依赖 Windows 上存在 `python3.13` 命令别名。NDK 的 Windows linker/archiver 使用 `.cmd`、`.exe` 后缀。
- 原生链接参数包含 `-Wl,-z,max-page-size=16384`。其适用范围仅限这三个自行编译的 wheel。

Android 裁剪 Playwright、CUDA/Hugging Face 推理模型、MarkItDown/Magika/ONNX、上游终端/git/PDF 等当前路由不用的依赖。Office 改用独立进程中的 mammoth、markdownify、openpyxl 和 defusedxml。NumPy 使用 1.26.2；LibCST 固定 0.3.23 并补齐 `typing-inspect`、`mypy-extensions`；trafilatura 固定 2.0.0。完整裁剪集合和覆盖版本由准备工具集中列出，不通过 pip 的自动依赖解析重新引入桌面包。

## 构建和安装

在当前终端设置 `JAVA_HOME` 指向 JDK 17，`ANDROID_HOME` 指向 SDK；Gradle 需要能找到上述 SDK/NDK。准备好前端依赖后执行：

```powershell
npm.cmd ci
npm.cmd run build
$env:TAVERN_BUILD_PYTHON = (Resolve-Path .cache/android/build-env/Scripts/python.exe).Path
& .cache/android/gradle-8.13/bin/gradle.bat -p packaging/android --no-daemon assembleDebug
```

若 Gradle 安装在其他位置，替换命令中的可执行路径。默认 APK 输出到 `.cache/android/build/app/outputs/apk/debug/app-debug.apk`。构建完成后可用 SDK Build Tools 的 `apksigner verify --verbose`、`zipalign -c -v 4` 核对签名和 ZIP 对齐；ZIP 对齐检查不等于 16 KiB ELF 内存页兼容。

Gradle/JVM 不自动读取 `HTTP_PROXY`、`HTTPS_PROXY`。如果 Maven 等下载也需要代理，在当前调用中分别传入 HTTP 与 HTTPS 的 JVM 参数，不必改全局 Gradle 配置。PowerShell 中必须把每个 `-D...` 参数整体加引号：

```powershell
& .cache/android/gradle-8.13/bin/gradle.bat "-Dhttp.proxyHost=<代理主机>" "-Dhttp.proxyPort=<代理端口>" "-Dhttps.proxyHost=<代理主机>" "-Dhttps.proxyPort=<代理端口>" -p packaging/android --no-daemon assembleDebug
```

```powershell
adb install -r .cache/android/build/app/outputs/apk/debug/app-debug.apk
```

`-r` 用于相同签名下保留数据更新。不同电脑的默认 debug 密钥可能不同；更换签名前先通过应用备份数据，不把卸载当成无损升级。

## 日常 USB 开发

准备好上述构建环境、连接一台已授权 USB 调试的手机后，从项目根目录执行：

```powershell
.cache/android/build-env/Scripts/python.exe tools/android/dev.py check
```

该命令依次进行离线增量构建、ADB 覆盖安装和轻量真机自检。默认使用独立的 `dev` [构建变体](https://developer.android.com/build/build-variants)：包名为 `com.terrarium.tavern.dev`，手机上显示“Terrarium Tavern 开发版”，使用自己的应用数据目录。个人使用包仍为 `com.terrarium.tavern`。自动自检只在开发版的临时目录里创建一份测试存储，完成后清理测试数据并结束开发版进程。

自检通过 Android Instrumentation 在手机的 Chaquopy Python 与实际脚本 WebView 中运行，覆盖原生分词依赖、Half Agent 与 Full Agent 的角色卡导入、开场变量初始化及对话读取。它不使用个人设置、API 密钥或模型权重，不调用收费模型 API；也不代表完整剧情、模型请求、界面布局和本地模型性能已经验证。探针的 Java、Python 源码和 Instrumentation 入口只打包到 `dev` 变体。

| 命令或参数 | 用途 |
|---|---|
| `doctor` | 查看连接状态、设备 ABI、API 级别、SoC、内存页与可见内存 |
| `build` | 只增量构建开发版 APK，无需连接手机 |
| `deploy` | 构建并覆盖安装开发版，不运行自检 |
| `check` | 构建、覆盖安装并运行三项轻量自检 |
| `smoke --suite sessions` | 对已安装的开发版只运行两种会话初始化检查 |
| `smoke --suite native` | 对已安装的开发版只检查原生分词依赖 |
| `logs` | 读取开发版主进程及辅助进程的日志 |
| `logs --target app` | 只读采集个人使用包的日志 |
| `deploy --target app` | 明确构建并更新个人使用包；不会自动运行测试或清空数据 |
| `--web` | 修改 `web/`、`compat/` 或前端构建输入后，同时重建前端与 SDK |
| `--serial <序列号>` | 多设备时指定目标，工具不会任意选择手机 |
| `--online` | 明确允许 Gradle 下载缺失依赖；沿用当前终端的 HTTP/HTTPS 代理环境变量 |

只改 Python 或 Java 时直接增量构建，前端资源沿用上次构建结果；修改前端必须加 `--web`。工具不执行全量测试、不运行依赖准备流程，也不保持 Gradle daemon 或测试服务常驻。自检会结束开发版进程，因此不要同时在开发版中进行手动调试。开发版主 WebView 开启了远程调试，可通过电脑 Chrome 的 `chrome://inspect` 查看 Console 与 Network；个人使用包没有新增调试入口。

厂商安装器可能要求在手机上确认或验证身份。安装超过 90 秒未返回时，工具报告“安装状态未知”，不自动重复传包；在手机上确认已安装后可直接运行 `smoke` 继续。使用 `deploy --target app` 更新个人使用包前，应按[维护指南](../guides/maintenance.md)导出备份并完成或取消当前生成。

输出固定保存在受忽略的 `.cache/android/dev/`，每类只保留最近一次：`build.log`、`install.log`、`smoke.log`、`logcat.txt` 和带各阶段耗时的 `report.json`。日志按目标应用 UID 采集，覆盖 `:realm` 与 `:documents`，不清空系统日志缓冲区。失败会返回非零退出码并保留异常原文；构建成功与真机自检成功分别记录，不能用其中一个替代另一个。

`.cache/android/distribution-assets/third-party/` 随 APK 打包，包含原始 METADATA、wheel 内许可、项目许可、wheel SHA-256 清单和来源记录。SudachiPy 0.6.11 的 sdist 未包含独立 LICENSE 文件，工具另外保存其[固定上游 Git blob](https://api.github.com/repos/WorksApplications/sudachi.rs/git/blobs/261eeb9e9f8b2b4b0d119366dda99c6fd7d35c64)中的 Apache-2.0 文本，哈希为 `c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4`。

这些清单记录技术来源，不代表公开发行授权审查通过。html2text 的 GPL 与项目非商业授权之间的发行兼容性仍按[许可维护](licensing.md)记录为未决。缓存、构建环境、签名密钥和生成资产不纳入仓库；构建步骤可复用，但不同签名密钥、工具或 ZIP 元数据可能改变最终 APK 哈希。

## 手机端运行边界

导入角色卡、世界书、备份和制卡参考文件时，应用打开 Android 系统文件选择器，显示所有可打开的文件，再由对应导入流程校验格式与内容。不同文件提供方可能把 JSON 标成文本或二进制文件，CHARX 也没有标准 MIME 类型，因此 Android 选择器不按 MIME 类型筛掉文件。选择文件即通过系统存储访问框架（SAF）授予本次读取权限，无需申请“所有文件访问”或额外存储权限。可以从选择器进入手机存储的 `Download` 等公共目录；Android 11 及以上的 `Android/data`、`Android/obb` 等系统限制目录仍无法通过此选择器访问，需要先用文件管理器把待导入文件复制到公共目录。

首次启动选择本机可用端口并保存，后续启动复用同一 origin，使阅读设置和侧栏偏好继续有效。端口被占用时明确报错，不悄悄换 origin。每次后端启动生成新令牌，所有 API 和静态资源都需要认证。

主 WebView 的 HTTP(S) 请求仅允许当前本地 origin。剧情图片继续经既有图片缓存/代理；参谋正文等其他页面中的远程直链图片不会直接联网。普通外链由用户点击后交给系统浏览器，避免 loopback cookie 被其他端口接收。

脚本在独立 `:realm` 进程运行，Office 在独立 `:documents` 进程解析，超时或取消可以终止对应进程。脚本桥以 JSON 传递调用参数和结果；兼容层初始化仅确认完成，返回的 API、函数和库对象保留在脚本环境，避免把包含 `BigInt` 等非 JSON 值的运行时对象传回主进程。Office 保留有界读取、原字符分页和公式缓存限制，见[制卡 agent](../guides/card-author.md)。应用后端由可见的前台服务承载；停止服务会取消故事、制卡和参谋任务，并让存储收尾。系统终止进程后依靠既有原子提交与重启恢复，不能承诺设备厂商始终保留后台进程。
