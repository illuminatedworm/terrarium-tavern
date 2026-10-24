# Android 本地开发环境

此目录长期保存 Android 开发所需的工具和文件，不属于 `.cache/`，清理临时产物时不要删除。
只有本说明纳入 Git，其余内容均被忽略；目录丢失后按开发说明重新准备。

| 路径 | 用途 |
| --- | --- |
| `python/`、`build-env/` | Android 专用宿主 Python 及构建依赖 |
| `jdk/`、`sdk/`、`gradle-8.13/` | JDK 17、SDK/NDK/ADB 和 Gradle |
| `cargo/`、`rustup/` | 固定 Rust 工具链和交叉编译依赖 |
| `downloads/`、`native-sources/`、`python-prefix/` | 已校验下载、原生扩展源码和 Python 交叉编译 sysroot |
| `wheels/`、`requirements.txt`、`*-builds.json`、`wheel-manifest.json` | Android wheelhouse、锁定清单及来源记录 |
| `llama-jni/`、`distribution-assets/` | APK 的原生引擎和第三方许可资产 |
| `model-probe/` | 模型转换工具、GGUF 权重、推理探针及其记录 |
| `gradle-home/`、`gradle-project/`、`build/` | Gradle 依赖、增量构建状态和 APK |
| `dev/` | 最近一次开发构建、安装和真机自检记录 |

从零准备、构建和安装步骤见 [Android 开发说明](../docs/development/android.md)。
源码与构建脚本位于 `packaging/android/` 和 `tools/android/`；本目录不存放故事或用户设置。
