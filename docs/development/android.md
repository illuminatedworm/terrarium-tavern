# Android 开发

Android 版把 Python/FastAPI 后端、Vue 前端和脚本 WebView 装入同一应用，故事数据保存在手机应用私有目录，不连接电脑后端。GitHub Release 提供 debug 签名的侧载 APK，供个人安装使用，不用于应用商店发行。

## 设备范围

- Android 9（API 28）及以上，`arm64-v8a`，需要较新的 Android System WebView；缺少所需特性时应用提示更新。
- **仅支持 4 KiB 内存页设备。** libfreetype、libxml2、libexslt、libxslt、jiter、kohakuvault、rpds 仍是 4 KiB 的 ELF 加载段对齐；支持 16 KiB 设备需要重新构建这些依赖并验证完整 APK。
- 本地向量与重排只接受两个固定的 Qwen3 0.6B Q8_0 GGUF，可分别选择 CPU、OpenCL GPU（API 34+）或实验性 Hexagon NPU（API 34+），权重单独导入。

`dev.py doctor` 可查看已连接设备的 ABI、API 级别与内存页大小。

## 与桌面的边界

**Android 工作不得改变桌面（Windows/Linux）的功能、界面、启动方式与默认行为**；确需改变（含安全加固）先说明动机与影响并取得用户明确同意。改动先按下面三类定位。

### 两端共用：改一处，两端同时生效

| 范围 | 内容 |
| --- | --- |
| 后端业务 | `src/tavern/` 中除下表分叉点以外的全部代码：回合编排、Writer/Tracker、记忆、世界书检索与注入、提示词、模型请求、制卡 agent、参谋 agent、MVU |
| 持久格式 | 数据库 schema、设置文档版本、个人数据包；Android 备份可导入桌面，反之亦然。格式变化须两端都能升级 |
| 界面 | `web/`、`compat/`、`vendor/mvu/` 只有一份，APK 打包同一次 `npm run build` 的产物 |
| 脚本隔离约定 | [`realm.py`](../../src/tavern/scripting/realm.py) 的后端接口，以及 [`network_guard.py`](../../src/tavern/scripting/network_guard.py) 的文档起始脚本（桌面受控窗口与 Android WebView 共用） |
| 依赖与版本 | `uv.lock`、Python 3.13 基准、应用版本号；Android 从 `uv.lock` 导出后裁剪，不另设锁文件 |

改共用代码时核对桌面输出不变，并确认 Android 裁剪的依赖（见[固定来源与适配](#固定来源与适配)）没有被新代码引用。

### 平台分叉点：只在这些位置分支

后端以 [`is_android()`](../../src/tavern/android/platform.py) 判断平台，它只读取 Android 宿主设置的 `TAVERN_PLATFORM=android`；前端以 `window.TavernAndroid` 桥或 `/api/health` 的 `platform` 判断。新的平台差异应加入这些位置之一，或新增同样窄的边界，不在业务代码中散布判断。

| 位置 | 桌面 | Android |
| --- | --- | --- |
| [`application/api.py`](../../src/tavern/application/api.py) `create_app` | 本地访问边界 | 另加启动令牌认证、固定 Host、CSP 与 `AndroidFiles` 缓存策略 |
| [`scripting/realm.py`](../../src/tavern/scripting/realm.py) `default_backend` | Playwright Chromium | [`android_realm.py`](../../src/tavern/scripting/android_realm.py)，`:realm` / `:realm_tools` 进程 |
| [`search/local_models.py`](../../src/tavern/search/local_models.py) `create_local_model_manager` | CUDA/Hugging Face 子进程 | [`android/local_models.py`](../../src/tavern/android/local_models.py)，llama.cpp GGUF |
| [`character/editor_files.py`](../../src/tavern/character/editor_files.py) | MarkItDown | [`android/documents.py`](../../src/tavern/android/documents.py)，`:documents` 进程 |
| [`chatgpt/vault.py`](../../src/tavern/chatgpt/vault.py) | Windows DPAPI / Linux 明文加权限 | Android Keystore AES-GCM |
| [`android/platform.py`](../../src/tavern/android/platform.py) `open_browser` | `webbrowser` | 系统浏览器 Intent |
| [`web/lib/download.ts`](../../web/lib/download.ts) | 普通下载链接 | 系统保存对话框 |
| [`web/lib/hostCapabilities.ts`](../../web/lib/hostCapabilities.ts) | 卡片脚本照常运行 | WebView 不能隔离网络时暂停卡片脚本 |
| [`web/lib/hostCapabilities.ts`](../../web/lib/hostCapabilities.ts) `androidHost` | `<html>` 无标记 | 加 `android-host`：游玩页输入框离开底部时收起，跳转胶囊、历史轨与跳到最新箭头更淡 |
| [`LocalResourceSettings.vue`](../../web/components/settings/LocalResourceSettings.vue) | 下载 CUDA 模型 | 导入 GGUF、选择运行设备 |
| [`web/styles/tokens.css`](../../web/styles/tokens.css) `--safe-*` | 恒为 0 | 宿主写入状态栏、手势条和挖孔内边距 |

### 单端专属：互不影响

| 平台 | 位置 |
| --- | --- |
| 仅 Android | `src/tavern/android/`（两端都导入的 `platform.py` 除外）、`scripting/android_realm.py`、`packaging/android/`、`tools/android/`、`android-dev/` |
| 仅桌面 | `desktop.py` 受控窗口、`setup`/`start`/`run-dev` 脚本、`scripting/playwright_realm.py`、CUDA 与 MarkItDown 依赖 |

桌面的 `setup` 与 `npm run build` 不涉及 Android 工具链；Android 构建不修改桌面 `.venv`。

## 构建环境

当前工具面向 Windows 本地构建，工具链全部装在项目内的 [`android-dev/`](../../android-dev/README.md)，不改系统 PATH、默认 Python 或桌面 `.venv`。该目录长期保留，不随 `.cache/` 清理；只有目录说明纳入 Git。

| 工具 | 固定要求 |
| --- | --- |
| Python | 3.13.15 |
| JDK | Temurin 17.0.17+10 |
| Android SDK | Platform 35、Build Tools 35.0.0 |
| Android NDK | `27.2.12479018` |
| Rust | `1.99.1`，`aarch64-linux-android` target |
| Gradle | 8.13 |
| Node/npm、uv | 前端构建与 `uv.lock` 导出 |
| Visual Studio Build Tools | C++ 工具与 Windows SDK，Rust 宿主构建需要；须自行安装 |

已安装 uv 和 Python 3.11+ 后，从项目根目录准备环境：

```powershell
python tools/android/setup.py --accept-sdk-licenses
```

该命令下载并校验固定的 JDK、Gradle、SDK 命令行工具和 Rust 安装器，安装 SDK/NDK 与 Rust target，并建立专用 Python 与 `build-env`。省略 `--accept-sdk-licenses` 时按 SDK 管理器提示处理许可。失败后可重跑，已通过哈希校验的下载会复用。

构建工具读取当前终端的 `HTTP_PROXY`、`HTTPS_PROXY`。`--ndk` 可指向其他 NDK；Rust 尊重环境中的 `CARGO_HOME`、`RUSTUP_HOME`。旧的 `.cache/android/` 不再使用。

## 准备依赖

```powershell
android-dev/build-env/Scripts/python.exe tools/android/prepare.py --prefer-pypi-native
```

该命令依次从固定 APK 恢复原生依赖、编译三个扩展、按 Android 裁剪 `uv export` 清单、构建 Jieba 与固定 KohakuTerrarium 源码 wheel，再下载其余 `android_24_arm64_v8a`、`cp313` wheel。产物在 `android-dev/wheels/`；最终的 `android-dev/requirements.txt` 带 `--no-index`、`--require-hashes`，Gradle 只消费本地 wheelhouse。

已有产物校验通过后复用；未知来源、哈希冲突或未声明的源码修改会报错。迁入此前另行验证的本地编译 wheel 时传 `--reuse-existing-builds`。各阶段也可单独执行：

```powershell
android-dev/build-env/Scripts/python.exe tools/android/recover_native.py --skip-pypi-native
android-dev/build-env/Scripts/python.exe tools/android/build_wheels.py --jobs 4
android-dev/build-env/Scripts/python.exe tools/android/prepare.py --receipts-only
```

`--receipts-only` 只为已有 wheelhouse 重新生成哈希清单和许可资产。

## 固定来源与适配

应用使用的 KohakuTerrarium **始终是 2.1.5**，源码固定在 [`e4fb517939ba07086481251717e888cb74dc19a4`](https://github.com/Kohaku-Lab/KohakuTerrarium/tree/e4fb517939ba07086481251717e888cb74dc19a4)。

原生依赖的备用来源固定为 [KohakuTerrarium 2.1.7 Android APK](https://github.com/Kohaku-Lab/KohakuTerrarium/releases/download/v2.1.7/kohakuterrarium-2.1.7-android.apk)（SHA-256 `1234727d90444b841e947ddd0a6970d96b57cdc36b97b20e1073b79516996835`），只提取依赖二进制，不升级上述源码。`--prefer-pypi-native` 改用 PyPI 的 MarkupSafe 3.0.4 与 KohakuVault 0.8.6 Android wheel。

tiktoken 0.14.0、SudachiPy 0.6.11、pydantic-core 2.41.5 取自 `uv.lock` 的已校验 sdist，使用 [Chaquopy Python 3.13.7 sysroot](https://repo.maven.apache.org/maven2/com/chaquo/python/python/3.13.7/python-3.13.7-aarch64-linux-android.tar.gz)（SHA-256 `0d889ce4237b0e37f3c8b45e6094aec693bb5faaf5cbdfb7167e16fedbc1a40e`）交叉编译：

- tiktoken 与 SudachiPy 改用 maturin；SudachiPy 的插件 `.so` 命名条件增加 Android，脚本只接受已核对的补丁结果。
- tiktoken 与 SudachiPy 使用 `tools/android/native-locks/` 中的固定 Cargo.lock，pydantic-core 沿用 sdist 自带锁文件，统一 `--locked` 编译。
- 可用 `build_wheels.py --package <名称>` 单独重建。

Android 裁剪 Playwright、CUDA/Hugging Face 推理、MarkItDown/Magika/ONNX 等不用的依赖，Office 改用 mammoth、markdownify 与 openpyxl。裁剪集合与覆盖版本集中在 `tools/android/prepare.py`。

## 构建和安装

### 版本号与内部序号

Android 的两项版本配置均在 [`packaging/android/app/build.gradle`](../../packaging/android/app/build.gradle) 中维护：

| 配置 | 用途 | 示例 |
| --- | --- | --- |
| `versionName` | 显示给用户的应用版本，采用三位版本号 | `0.8.0` |
| `versionCode` | Android 比较安装包新旧的内部序号，必须是正整数 | `3` |

两个值没有换算关系。相同的 `versionName` 可以对应先后打出的多个 APK；例如 `0.8.0 / 3`、`0.8.0 / 4`，以后应用升到 `0.9.0` 时内部序号继续用 `5`，不重置。Android 通常会阻止安装内部序号更低的旧包；覆盖升级还要求包名和签名相符。原理见 [Android 官方版本说明](https://developer.android.com/studio/publish/versioning)。

本项目约定：

- **用户每次明确要求打包 APK 时，先将 `versionCode` 在当前值上加一，再构建。** 读取并修改上述 Gradle 配置，作为后续递增的唯一记录；不从日期、Git 提交数或 APK 文件名推算。
- 同一次打包要求内的构建失败重试、签名或校验重跑共用同一内部序号，不重复增加；已经使用的序号不回退。
- 普通开发编译、自动验证、文档修改，以及仅讨论打包方式时不增加内部序号。
- `versionName` 保持不变，除非用户另行要求升级应用版本；按[版本号基准](../../.agents/VERSIONING.md)同步维护应用版本。
- 交付 APK 前核对包内的 `versionName`、`versionCode` 和签名；文件名附加对应的 `build<versionCode>`，并在交付说明中写明显示版本与内部序号。

### 手动构建

日常开发使用下一节的 `dev.py`。需要手动构建个人使用包时：

```powershell
$env:JAVA_HOME = (Resolve-Path android-dev/jdk/jdk-17.0.17+10).Path
$env:ANDROID_HOME = (Resolve-Path android-dev/sdk).Path
$env:GRADLE_USER_HOME = Join-Path $PWD 'android-dev/gradle-home'
npm.cmd ci
npm.cmd run build
android-dev/build-env/Scripts/python.exe tools/android/prepare_llama.py
$env:TAVERN_BUILD_PYTHON = (Resolve-Path android-dev/build-env/Scripts/python.exe).Path
& android-dev/gradle-8.13/bin/gradle.bat -p packaging/android --project-cache-dir (Join-Path $PWD 'android-dev/gradle-project') --no-daemon --max-workers=1 --no-watch-fs assembleDebug
& android-dev/sdk/platform-tools/adb.exe install -r android-dev/build/app/outputs/apk/debug/app-debug.apk
```

`-r` 在相同签名下保留数据更新；不同电脑的默认 debug 密钥可能不同，更换签名前先在应用内备份数据。

Gradle 不读取 `HTTP_PROXY`、`HTTPS_PROXY`，需要代理时传 JVM 参数（PowerShell 中每个 `-D...` 整体加引号）：

```powershell
& android-dev/gradle-8.13/bin/gradle.bat "-Dhttp.proxyHost=<主机>" "-Dhttp.proxyPort=<端口>" "-Dhttps.proxyHost=<主机>" "-Dhttps.proxyPort=<端口>" -p packaging/android --no-daemon assembleDebug
```

## 日常 USB 开发

连接已授权 USB 调试的手机，解锁亮屏后执行：

```powershell
android-dev/build-env/Scripts/python.exe tools/android/dev.py check
```

该命令离线增量构建、覆盖安装并运行真机自检。默认目标是 `dev` 构建变体：包名 `com.terrarium.tavern.dev`，显示为“Terrarium Tavern 开发版”，数据目录独立；个人使用包为 `com.terrarium.tavern`。自检代码只打包进 `dev` 变体，不使用个人设置、API 密钥或模型权重。

| 命令或参数 | 用途 |
| --- | --- |
| `doctor` | 连接状态、ABI、API 级别、SoC、内存页与可见内存 |
| `build` | 只增量构建开发版，无需连接手机 |
| `deploy` | 构建并覆盖安装开发版 |
| `check` | 构建、安装并运行除 `devices` 与模型外的全部自检 |
| `smoke --suite <名称>` | 对已安装的开发版只运行一组自检，见下表 |
| `logs` | 读取开发版主进程及各辅助进程的日志 |
| `--target app` | 与 `deploy` 或 `logs` 合用，作用于个人使用包；不运行测试、不清空数据 |
| `--web` | 修改 `web/`、`compat/`、`vendor/` 后同时重建前端与 SDK |
| `--serial <序列号>` | 多设备时指定目标 |
| `--online` | 允许 Gradle 下载缺失依赖 |

| 自检组 | 内容 |
| --- | --- |
| `native` | 原生分词依赖 |
| `sessions` | Half Agent 与 Full Agent 的角色卡导入、开场变量初始化和对话读取 |
| `scripts` | 带脚本的消息帧、制卡展示断言与依赖打包 |
| `documents` | 制卡 agent 读取 DOCX/XLSX |
| `restart` | 后端停止后立即在原端口重启 |
| `ui` | 主界面宿主网络策略，不访问外网 |
| `devices` | 只探测加速设备，不加载权重 |
| `models`、`models-opencl`、`models-hexagon` | 本地模型，见[模型自检](#模型自检) |

自检期间保持开发自检页在前台，锁屏或熄屏会直接报错，解锁后可只运行 `smoke`。自检会结束开发版进程，不要同时手动调试。开发版主 WebView 开启远程调试，可在电脑 Chrome 的 `chrome://inspect` 查看。安装超过 90 秒未返回时报告“安装状态未知”，在手机上确认后可直接运行 `smoke`。

输出在 `android-dev/dev/`，每类只保留最近一次：`build.log`、`install.log`、`smoke.log`、`logcat.txt` 和 `report.json`。

更新个人使用包前，先按[维护指南](../guides/maintenance.md)导出备份并完成或取消当前生成。

## 应用内本地模型

用户侧的导入与设置见[检索指南](../guides/retrieval.md)，本节说明打包与运行约束。

`tools/android/prepare_llama.py` 校验固定的 llama.cpp [`b11829`](https://github.com/ggml-org/llama.cpp/releases/tag/b11829) CPU 与 Snapdragon 原生包。CPU 入口为 `libtavern_llama_server.so`；Snapdragon 库统一加 `libtavern_accel_` 前缀（Hexagon DSP skel 保持原名），两套库互不覆盖，权重共用。来源、修改前后哈希与第三方许可写入发行资产。`dev.py build/deploy/check` 离线调用此步骤，缺少原生包时报错，须先联网准备。

- 原生程序只从安装器提取的 `nativeLibraryDir` 启动，不执行可写目录中的文件。
- 每个模型独立进程、loopback 随机端口和临时认证密钥；取消、超时或停止服务结束对应进程，不影响另一个模型。
- 固定单槽、4096-token 上下文，超过时报错，不截断；世界书索引沿用 token 分片，已准入条目完整注入。
- 设备选择保存为 `cpu`、`opencl:0` 或 `hexagon:0`。切换设备不重新导入权重，但需重新预热，派生向量按设备与引擎包隔离。
- 加速设备在服务启动后由后台探测，探测完成前 CPU 可直接使用。加速推理失败时明确报错，不自动回退 CPU。界面显示实际设备与层卸载数量；层卸载不代表所有算子都在加速器上执行。
- Hexagon 是[上游实验性后端](https://github.com/ggml-org/llama.cpp/blob/184f6536024668e8908ab8c86033d56708e62b40/docs/backend/snapdragon/README.md)。reranker 在三种设备上都使用 `rank` pooling 读取 `P(yes)`。

### 模型文件

应用只接受 [`model_sources.json`](../../src/tavern/android/model_sources.json) 中固定 SHA-256 的两个文件：

- 向量：Qwen 官方 [`Qwen3-Embedding-0.6B-Q8_0.gguf`](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/370f27d7550e0def9b39c1f16d3fbaa13aa67728/Qwen3-Embedding-0.6B-Q8_0.gguf)。
- 重排：Qwen 没有官方 GGUF，须用固定 llama.cpp 转换器从官方权重生成，社区转换版哈希不同会被拒绝。先在装有本地检索依赖的桌面版中下载 Qwen3-Reranker-0.6B（版本 `main`），再运行 `.venv/Scripts/python.exe tools/android/model_probe_prepare.py`。

两个产物都在 `android-dev/model-probe/models/`。手机模型与派生向量独立于桌面缓存，不能混用。

### 模型自检

`--suite models` 用同一夹具比较 CPU、GPU、NPU 的真实导入、加载、跨微批输入、长条目索引、重排、取消与双模型共存，并记录耗时、RSS 峰值和与 CPU 的向量余弦相似度。判定条件：已知相关文档排序正确、同设备重复输入的向量稳定、重排最大绝对误差 ≤ 0.001；跨硬件数值差异本身不算失败。`models-opencl`、`models-hexagon` 只重跑对应后端，复用缓存的 CPU 对照；变更夹具或引擎后应重新生成对照。

运行前把两个模型文件放进开发版私有缓存：

```powershell
adb push android-dev/model-probe/models/Qwen3-Embedding-0.6B-Q8_0.gguf android-dev/model-probe/models/Qwen3-Reranker-0.6B-Q8_0.gguf /data/local/tmp/
adb shell run-as com.terrarium.tavern.dev mkdir -p cache/model-fixtures
adb shell run-as com.terrarium.tavern.dev cp /data/local/tmp/Qwen3-Embedding-0.6B-Q8_0.gguf /data/local/tmp/Qwen3-Reranker-0.6B-Q8_0.gguf cache/model-fixtures/
```

## 本地模型推理探针

`tools/android/model_probe.py` 不经过应用，直接在 Windows CPU 与 ADB 连接的手机上用同一 GGUF 推理，并与桌面 CUDA BF16 结果对照，用于区分引擎问题与应用集成问题。需要桌面 `.venv` 已装本地检索依赖、`data/models/` 有两份源模型、至少 2 GiB 空闲显存。

```powershell
$env:PYTHONUTF8 = "1"
.venv/Scripts/python.exe tools/android/model_probe_prepare.py
.venv/Scripts/python.exe tools/android/model_reference.py
.venv/Scripts/python.exe tools/android/model_probe.py
```

前两步只在来源或样本变化后重做；`model_probe.py` 支持 `--serial` 与 `--resume`。阈值见脚本的 `LIMITS`，不作为打包门槛；token、输出维度或首选文档不符时返回非零退出码。产物在 `android-dev/model-probe/`。

## 依赖许可资产

`android-dev/distribution-assets/third-party/` 随 APK 打包，包含原始 METADATA、wheel 内许可、项目许可、wheel 哈希清单和来源记录。SudachiPy 0.6.11 的 sdist 没有独立 LICENSE，工具另存其[固定上游 Git blob](https://api.github.com/repos/WorksApplications/sudachi.rs/git/blobs/261eeb9e9f8b2b4b0d119366dda99c6fd7d35c64)中的 Apache-2.0 文本。

这些清单记录技术来源，不代表发行授权审查已通过；html2text 的 GPL 与项目非商业授权的兼容性仍按[许可维护](licensing.md)记录为未决。

## 手机端运行约束

- **进程**：后端由可见的前台服务承载。故事脚本在 `:realm`、制卡验证与依赖打包在 `:realm_tools`、Office 解析在 `:documents` 进程运行，超时或取消只终止对应进程。停止服务会取消故事、制卡和参谋任务并让存储收尾。
- **访问边界**：首次启动选定端口并保存，之后复用同一 origin，使阅读设置和侧栏偏好保持有效；端口被占用时报错，不更换 origin。每次启动生成新令牌，API 与静态资源都需认证（见[架构](../design/architecture.md)）。主 WebView 只请求本地 origin，外链交给系统浏览器。
- **文件导入**：使用系统文件选择器，不按 MIME 类型过滤，由导入流程校验内容；`Android/data` 等受限目录需先复制到 `Download` 等公共目录。
- **显示**：Android 11+ 界面铺满系统栏，内边距经 `--safe-*` 变量传入；键盘弹出时 WebView 缩到键盘上方。应用更新后首次启动清空 WebView 缓存。
- **后台冻结**：前台服务仍受厂商冻结与省电策略约束，期间请求可能暂停或超时报错，失败回合不部分提交。进程被结束后，重新打开会把未完成回合标记为中断，可按界面入口继续或重试。应用不修改设备的冻结或省电规则。
- **长世界书**：手机 CPU 可能超过默认每条 60 秒的索引时限，可在“检索 → 高级参数”调整“单条索引超时（秒）”。

## 维护约束

- 平台差异只放在[平台分叉点](#平台分叉点只在这些位置分支)；Android 构建步骤只放在 `tools/android/` 和 `packaging/android/`。
- 不提供“手机作为客户端连接电脑后端”的远程模式，它与[架构](../design/architecture.md)中服务只监听 loopback 的约定冲突。
- Chaquopy 中不能用 `sys.executable` 或 `multiprocessing` 启动 Python 子进程，Playwright 也无法启动 Chromium；需要隔离的工作放进独立 Android 进程的 Service。同一进程的 WebView 共用渲染进程，脚本死循环会拖住同进程页面，因此故事脚本与制卡工具分属 `:realm` 和 `:realm_tools`。
