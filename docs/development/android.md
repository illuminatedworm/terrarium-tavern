# Android APK 构建

Android 版把 Python/FastAPI 后端、Vue 前端和脚本 WebView 装入同一应用，故事数据保存在手机应用私有目录，不需要连接电脑上的后端。当前产物为私人安装用的 debug 签名构建，不是公开发行。

## 设备范围

- Android 9（API 28）及以上，`arm64-v8a`，需要较新的 Android System WebView；缺少所需特性时应用会提示更新。
- 主界面和后台脚本共用文档起始网络隔离，禁止顶层与嵌套文档恢复 WebRTC；主界面缺少这项 WebView 能力时暂停交互卡片并提示更新，静态内容仍可访问。`dev.py smoke --suite ui` 单独验证主界面使用的宿主策略，覆盖五类文档与回环 UDP 监听，不访问外网。
- **仅支持 4 KiB 内存页设备。** libfreetype、libxml2、libexslt、libxslt、jiter、kohakuvault、rpds 仍是 4 KiB 的 ELF 加载段对齐；支持 16 KiB 设备需要重新构建这些依赖并验证完整 APK。
- 本地向量与重排使用固定版本的 Qwen3 0.6B CPU/Q8_0 GGUF，权重单独导入，不打进 APK；已有 CUDA 配置保留，需在界面显式切换。关键词与 BM25 沿用桌面的 Jieba、Sudachi 和 core 词典。

`dev.py doctor` 可查看已连接设备的 ABI 与内存页大小。

## 构建环境

当前工具面向 Windows 本地构建。先准备以下工具，脚本不会安装或修改系统工具链：

| 工具         | 固定要求                                        |
| ------------ | ----------------------------------------------- |
| Python       | 3.13；项目版本见 `.python-version`              |
| JDK          | 17                                              |
| Android SDK  | Platform 35、Build Tools 35.0.0                 |
| Android NDK  | `27.2.12479018`                                 |
| Rust         | `1.99.1`，已安装 `aarch64-linux-android` target |
| Gradle       | 8.13                                            |
| Node/npm、uv | 用于已有前端构建与 `uv.lock` 导出               |

默认缓存根为 `.cache/android/`。NDK 默认位于 `.cache/android/sdk/ndk/27.2.12479018`，可给准备工具传 `--ndk` 指向其他位置。Rust 默认使用 `.cache/android/cargo` 和 `.cache/android/rustup`，也尊重环境中的 `CARGO_HOME`、`RUSTUP_HOME`。

从项目根目录创建专用构建环境：

```powershell
uv venv --python 3.13 .cache/android/build-env
uv pip install --python .cache/android/build-env/Scripts/python.exe --index-url https://pypi.org/simple pip==26.2.2 maturin==1.15.0 setuptools-rust==1.13.0 setuptools==84.2.1 wheel==0.48.0 packaging==26.3
```

构建工具读取当前终端的 `HTTP_PROXY`、`HTTPS_PROXY`，不改全局 pip、uv 或系统代理配置。

Android 合并后的桌面开发环境也统一使用 Python 3.13。已有 Windows/Linux 环境须按[升级说明](../guides/maintenance.md#备份升级与回退)重新运行相应 `setup`；uv 会重建不兼容的 `.venv`。启用本地检索时保留安装开关，torch 等可选依赖需重新安装；Android 专用 `build-env` 与桌面 `.venv` 分别管理。

## 准备依赖

```powershell
.cache/android/build-env/Scripts/python.exe tools/android/prepare.py --prefer-pypi-native
```

该命令依次从固定 APK 恢复原生依赖、编译三个扩展、按 Android 平台裁剪 `uv export` 清单、构建 Jieba 与固定 KohakuTerrarium 源码 wheel，再下载其余 `android_24_arm64_v8a`、`cp313` wheel。产物保存在 `.cache/android/wheels/`；sdist 按 `uv.lock` 校验，PyPI wheel 按锁文件或固定版本的官方摘要校验。最终 `.cache/android/requirements.txt` 带 `--no-index`、`--require-hashes` 与每个 wheel 的哈希，Gradle 只消费本地 wheelhouse。

记录匹配的已有产物校验后复用；未知来源、哈希冲突或未声明的源码修改会报错，不自动删除缓存。迁入此前另行验证的本地编译 wheel 时传 `--reuse-existing-builds`，来源记录会注明未重新编译。各阶段也可单独执行：

```powershell
.cache/android/build-env/Scripts/python.exe tools/android/recover_native.py --skip-pypi-native
.cache/android/build-env/Scripts/python.exe tools/android/build_wheels.py --jobs 4
.cache/android/build-env/Scripts/python.exe tools/android/prepare.py --receipts-only
```

`--receipts-only` 不下载或编译，只为已有 wheelhouse 重新生成哈希锁定清单和许可资产。

## 固定来源与适配

应用使用的 KohakuTerrarium **始终是 2.1.5**，源码固定在 [`e4fb517939ba07086481251717e888cb74dc19a4`](https://github.com/Kohaku-Lab/KohakuTerrarium/tree/e4fb517939ba07086481251717e888cb74dc19a4)。

原生依赖的备用来源固定为 [KohakuTerrarium 2.1.7 Android APK](https://github.com/Kohaku-Lab/KohakuTerrarium/releases/download/v2.1.7/kohakuterrarium-2.1.7-android.apk)，SHA-256 为 `1234727d90444b841e947ddd0a6970d96b57cdc36b97b20e1073b79516996835`。它只提供依赖二进制，不升级上述源码。恢复器只提取列出的依赖根目录与发行元数据，保留许可证、bytecode 与 `.so` 原内容，重建 WHEEL/RECORD 并添加来源说明。`--prefer-pypi-native` 改用 PyPI 的 MarkupSafe 3.0.4 与 KohakuVault 0.8.6 Android wheel。

自行编译的 tiktoken 0.14.0、SudachiPy 0.6.11、pydantic-core 2.41.5 取自 `uv.lock` 的已校验 sdist，使用 [Chaquopy Python 3.13.7 sysroot](https://repo.maven.apache.org/maven2/com/chaquo/python/python/3.13.7/python-3.13.7-aarch64-linux-android.tar.gz)（SHA-256 `0d889ce4237b0e37f3c8b45e6094aec693bb5faaf5cbdfb7167e16fedbc1a40e`）交叉编译，链接参数含 `-Wl,-z,max-page-size=16384`：

- tiktoken 改用 maturin，同时打包 `tiktoken` 与 `tiktoken_ext`，构建时检查插件模块已进入 wheel；可用 `build_wheels.py --package tiktoken` 单独重建。
- SudachiPy 改用 maturin，`sudachi-lib/src/plugin/loader.rs` 的 `.so` 命名条件增加 Android；脚本只接受已核对的原文或精确补丁结果。
- tiktoken 与 SudachiPy 使用 `tools/android/native-locks/` 中的固定 Cargo.lock，pydantic-core 沿用 sdist 自带锁文件，统一 `--locked` 编译。

Android 裁剪 Playwright、CUDA/Hugging Face 推理、MarkItDown/Magika/ONNX 等当前路由不用的依赖；Office 改用独立进程中的 mammoth、markdownify 与 openpyxl。裁剪集合与覆盖版本（NumPy 1.26.2、LibCST 0.3.23、trafilatura 2.0.0 等）集中列在 `tools/android/prepare.py`。

## 构建和安装

在当前终端设置 `JAVA_HOME` 指向 JDK 17、`ANDROID_HOME` 指向 SDK，准备好前端依赖后执行：

```powershell
npm.cmd ci
npm.cmd run build
.cache/android/build-env/Scripts/python.exe tools/android/prepare_llama.py
$env:TAVERN_BUILD_PYTHON = (Resolve-Path .cache/android/build-env/Scripts/python.exe).Path
& .cache/android/gradle-8.13/bin/gradle.bat -p packaging/android --no-daemon assembleDebug
adb install -r .cache/android/build/app/outputs/apk/debug/app-debug.apk
```

APK 输出到 `.cache/android/build/app/outputs/apk/debug/app-debug.apk`。`-r` 在相同签名下保留数据更新；不同电脑的默认 debug 密钥可能不同，更换签名前先在应用内备份数据。

Gradle 不读取 `HTTP_PROXY`、`HTTPS_PROXY`。Maven 下载需要代理时在本次调用中传 JVM 参数，PowerShell 中每个 `-D...` 参数需整体加引号：

```powershell
& .cache/android/gradle-8.13/bin/gradle.bat "-Dhttp.proxyHost=<代理主机>" "-Dhttp.proxyPort=<代理端口>" "-Dhttps.proxyHost=<代理主机>" "-Dhttps.proxyPort=<代理端口>" -p packaging/android --no-daemon assembleDebug
```

## 日常 USB 开发

连接一台已授权 USB 调试的手机并解锁亮屏后，从项目根目录执行：

```powershell
.cache/android/build-env/Scripts/python.exe tools/android/dev.py check
```

该命令依次离线增量构建、ADB 覆盖安装并运行真机自检。默认目标是独立的 `dev` 构建变体：包名 `com.terrarium.tavern.dev`，显示为“Terrarium Tavern 开发版”，使用自己的数据目录；个人使用包仍为 `com.terrarium.tavern`。自检代码只打包进 `dev` 变体，不使用个人设置、API 密钥或模型权重。

自检通过 Android Instrumentation 运行，覆盖原生分词依赖；Half Agent 与 Full Agent 的角色卡导入、开场变量初始化和对话读取；带脚本的消息帧、制卡展示断言与依赖打包；制卡 agent 读取 DOCX/XLSX；以及后端停止后立即在原端口重启（会确认端口仍处于 TIME_WAIT）。重启检查启动开发版自己的后端与数据目录，其余检查只使用结束后即清理的临时目录。运行期间会显示开发自检页，请保持该页在前台，避免厂商后台冻结；锁屏或熄屏时会直接报错，解锁后可只运行 `smoke`。

| 命令或参数               | 用途                                                       |
| ------------------------ | ---------------------------------------------------------- |
| `doctor`                 | 查看连接状态、设备 ABI、API 级别、SoC、内存页与可见内存    |
| `build`                  | 只增量构建开发版 APK，无需连接手机                         |
| `deploy`                 | 构建并覆盖安装开发版，不运行自检                           |
| `check`                  | 构建、覆盖安装并运行自检                                   |
| `smoke --suite sessions` | 对已安装的开发版只运行两种会话初始化检查                   |
| `smoke --suite native`   | 对已安装的开发版只检查原生分词依赖                         |
| `smoke --suite scripts`  | 只运行消息帧、制卡展示断言与依赖打包检查                   |
| `smoke --suite documents` | 只运行制卡 agent 的 DOCX/XLSX 读取检查                    |
| `smoke --suite restart`  | 只运行后端停止后立即重启检查                               |
| `smoke --suite models`   | 对已安装的开发版运行本地模型检查，见下文                   |
| `logs`                   | 读取开发版主进程及各辅助进程的日志                         |
| `logs --target app`      | 只读采集个人使用包的日志                                   |
| `deploy --target app`    | 构建并更新个人使用包；不运行测试、不清空数据               |
| `--web`                  | 修改 `web/`、`compat/` 或前端构建输入后，同时重建前端与 SDK |
| `--serial <序列号>`      | 多设备时指定目标                                           |
| `--online`               | 允许 Gradle 下载缺失依赖，沿用终端代理环境变量             |

只改 Python 或 Java 时直接增量构建；修改前端必须加 `--web`。自检会结束开发版进程，不要同时在开发版中手动调试。开发版主 WebView 开启了远程调试，可在电脑 Chrome 的 `chrome://inspect` 查看。安装超过 90 秒未返回时，工具报告“安装状态未知”且不重复传包，在手机上确认安装后可直接运行 `smoke`。更新个人使用包前，先按[维护指南](../guides/maintenance.md)导出备份并完成或取消当前生成。

输出保存在受忽略的 `.cache/android/dev/`，每类只保留最近一次：`build.log`、`install.log`、`smoke.log`、`logcat.txt` 和带各阶段耗时的 `report.json`。

## 应用内本地模型

`tools/android/prepare_llama.py` 校验固定的 llama.cpp `b11829` Android 原生包，按白名单提取所需库并 strip，入口改名为 `libtavern_llama_server.so`，同时把来源、哈希与第三方许可写入发行资产。`dev.py build/deploy/check` 会离线调用此步骤，缺少原生包时报错。

Gradle 使用 `jniLibs.useLegacyPackaging=true`，安装器把原生代码提取到 `nativeLibraryDir`，应用只从这里启动入口，不执行可写目录中的程序（见 [Android 的可执行文件限制](https://developer.android.com/about/versions/10/behavior-changes-10#execute-permission)）。每个模型有独立进程、loopback 随机端口和临时认证密钥。取消、超时或停止服务会结束对应进程；引擎明确拒绝的请求（例如超过上下文）只报告错误，模型保持加载。

运行参数固定为 CPU 四线程、单槽、4096-token 上下文、512-token 微批，沿用保存的批量大小、向量维度和项目提示词。超过上下文时报错，不截断查询或提示词；世界书索引与重排沿用现有 token 分片，已准入条目仍完整注入。

Android 的 [`Process.destroyForcibly()` 继承实现](https://android.googlesource.com/platform/libcore/+/refs/heads/android16-release/ojluni/src/main/java/java/lang/Process.java)调用 `destroy()`，其[原生实现仅发送 SIGTERM](https://android.googlesource.com/platform/libcore/+/refs/heads/android16-release/ojluni/src/main/native/UNIXProcess_md.c)。应用通过系统 shell 在 `exec` 前取得自有子进程 PID；正常停止未退出时，明确发送 SIGKILL 并等待回收，避免超时后残留模型进程。此边界不依赖 root 或隐藏 Java API。

### 模型文件

应用只接受 `src/tavern/android/model_sources.json` 中固定 SHA-256 的两个文件：

- 向量：Qwen 官方 [`Qwen3-Embedding-0.6B-Q8_0.gguf`](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/370f27d7550e0def9b39c1f16d3fbaa13aa67728/Qwen3-Embedding-0.6B-Q8_0.gguf)。
- 重排：Qwen 没有发布官方 GGUF，`Qwen3-Reranker-0.6B-Q8_0.gguf` 需用固定 llama.cpp 转换器从官方权重生成；社区转换版哈希不同，会被拒绝。先在装有本地检索依赖的桌面版中下载 Qwen3-Reranker-0.6B（版本 `main`），再运行 `.venv/Scripts/python.exe tools/android/model_probe_prepare.py`。

转换工具同时下载向量文件，两个产物都位于 `.cache/android/model-probe/models/`。导入时应用在后台复制并核对完整 SHA-256，成功后原子发布；失败或取消不替换已验证的旧文件。手机模型目录与派生向量独立于桌面缓存，不能混用。

### 模型自检

`dev.py smoke --suite models` 验证真实 GGUF 流式导入、原生加载、批处理、跨微批输入、索引分片、重排及进程释放。界面导入直接发送文件流，后端不再先暂存 multipart；按最多 1 MiB 的写入块累计哈希，校验成功后原子替换。启动时清理未完成的模型临时文件，正式权重不属于这项清理。先把两个模型文件放进开发版私有缓存：

```powershell
adb push .cache/android/model-probe/models/Qwen3-Embedding-0.6B-Q8_0.gguf .cache/android/model-probe/models/Qwen3-Reranker-0.6B-Q8_0.gguf /data/local/tmp/
adb shell run-as com.terrarium.tavern.dev mkdir -p cache/model-fixtures
adb shell run-as com.terrarium.tavern.dev cp /data/local/tmp/Qwen3-Embedding-0.6B-Q8_0.gguf /data/local/tmp/Qwen3-Reranker-0.6B-Q8_0.gguf cache/model-fixtures/
```

其他自检范围不加载模型权重。

## 本地模型推理探针

`tools/android/model_probe.py` 不经过应用，直接在 Windows CPU 和 ADB 连接的手机上用同一 Q8_0 GGUF 推理，并与项目 `_infer` 路径生成的 CUDA BF16 结果对照，用于区分引擎性能与应用集成问题。前提：桌面 `.venv` 已安装本地检索依赖，`data/models/` 有清单指定版本的两份源模型，电脑至少有 2 GiB 空闲显存。

```powershell
$env:PYTHONUTF8 = "1"
.venv/Scripts/python.exe tools/android/model_probe_prepare.py
.venv/Scripts/python.exe tools/android/model_reference.py
.venv/Scripts/python.exe tools/android/model_probe.py
```

前两步只在来源或样本变化后重做；`model_probe.py` 支持 `--serial` 与 `--resume`。重排通过 `/embedding` 的 `rank` pooling 读取 `P(yes)`，固定引擎需要 `--override-kv qwen3.embedding_length_out=int:2` 才能读出两维分类结果，应用内使用相同参数。数值比较的参考阈值见 `model_probe.py` 的 `LIMITS`，不作为打包门槛；token、输出维度或首选文档不符时返回非零退出码。产物与报告位于受忽略的 `.cache/android/model-probe/`，手机端文件位于 `/data/local/tmp/terrarium-model-probe/`。

## 依赖许可资产

`.cache/android/distribution-assets/third-party/` 随 APK 打包，包含原始 METADATA、wheel 内许可、项目许可、wheel SHA-256 清单和来源记录。SudachiPy 0.6.11 的 sdist 没有独立 LICENSE，工具另存其[固定上游 Git blob](https://api.github.com/repos/WorksApplications/sudachi.rs/git/blobs/261eeb9e9f8b2b4b0d119366dda99c6fd7d35c64)中的 Apache-2.0 文本。

这些清单记录技术来源，不代表发行授权审查已通过；html2text 的 GPL 与项目非商业授权的兼容性仍按[许可维护](licensing.md)记录为未决。

## 手机端运行边界

导入文件使用系统文件选择器，不按 MIME 类型过滤（JSON 常被标成文本或二进制，CHARX 没有标准类型），由各导入流程校验内容；选择即授予本次读取权限，无需存储权限。`Android/data` 等系统受限目录无法选择，需先复制到 `Download` 等公共目录。

首次启动选择可用端口并保存，之后复用同一 origin，使阅读设置和侧栏偏好保持有效；端口被其他应用占用时报错，不更换 origin。每次后端启动生成新令牌，API 与静态资源都需要认证。主 WebView 只允许请求当前本地 origin：剧情图片经既有图片缓存读取，其他页面的远程直链图片不加载，外链交给系统浏览器。

Android 11 及以上界面铺满状态栏、手势条和挖孔区域，宿主把这些内边距写入页面的 `--safe-*` 变量，桌面浏览器中均为 0；键盘弹出时 WebView 缩到键盘上方。APK 内静态文件的修改时间固定，除带哈希的 `assets/` 外一律不缓存，应用更新后首次启动还会清空 WebView 缓存。

故事脚本在 `:realm` 进程运行，制卡验证与依赖打包在 `:realm_tools` 进程运行，Office 在 `:documents` 进程解析；超时或取消只终止对应进程。脚本桥以 JSON 传递参数和结果，兼容层安装后返回的 API 对象留在脚本环境中。后端由可见的前台服务承载；停止服务会取消故事、制卡和参谋任务并让存储收尾。系统终止进程后依靠原子提交与重启恢复。

前台服务仍受设备的后台冻结与省电策略约束。冻结期间模型请求和本地接口可能暂停，回到前台后恢复，也可能因连接超时而报错；失败回合不会部分提交。进程被结束后，重新打开应用会将未完成回合标记为中断，并保留已提交剧情；按界面的继续或重试入口恢复工作。应用不修改设备的冻结或省电规则。

## 维护约束

- 桌面（Windows/Linux）通过独立受控窗口运行交互卡片，启动与退出方式见[维护指南](../guides/maintenance.md)。平台差异收在可替换的边界后面：`scripting/realm.py` 的脚本后端、`search/local_models.create_local_model_manager` 与 `android/platform.py`；Android 构建步骤只放在 `tools/android/` 和 `packaging/android/`，桌面的 `setup` 与 `npm run build` 不涉及 Android 工具链。
- 不提供“手机作为客户端连接电脑后端”的远程模式，它与[架构](../design/architecture.md)中服务只监听 loopback 的约定冲突。
- Chaquopy 中不能用 `sys.executable` 或 `multiprocessing` 启动 Python 子进程，Playwright 也无法启动 Chromium；需要隔离的工作放进独立 Android 进程的 Service。同一进程的 WebView 共用渲染进程，脚本死循环会拖住同进程页面，重置也会让该进程内所有脚本环境失效，因此故事脚本与制卡工具分属 `:realm` 和 `:realm_tools`。
- 手机 CPU 上长世界书可能超过默认每条 60 秒的索引时限；在目标模式的“检索 → 高级参数”中显式调整“单条索引超时（秒）”，范围为 1–3600 秒。查询与重排分别使用各自的超时设置。
