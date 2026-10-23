# 第三方声明

本文记录本项目使用或参考的第三方组件、规范及其来源。各组件适用各自的许可，具体条款以相应许可原文为准。

本项目原创部分适用 [Terrarium Tavern 非商业许可 1.1](PROJECT_LICENSE.md)，严格禁止任何形式的商业用途；该限制不覆盖或替代以下第三方组件各自的许可。完整范围见[许可说明](README.md)。本文的仓库相对链接按源码包目录组织。

## Kohaku Terrarium

- 项目来源：[Kohaku-Lab/KohakuTerrarium](https://github.com/Kohaku-Lab/KohakuTerrarium)
- 固定提交：`e4fb517939ba07086481251717e888cb74dc19a4`（2.1.5）
- 版权所有：2024–2026 Shih-Ying Yeh（KohakuBlueLeaf）及贡献者。
- 许可名称：**KohakuTerrarium License 1.0**。

本项目将 Kohaku Terrarium 作为依赖安装，并在应用名称与可见署名中保留 Terrarium 标识。其代码继续适用上游许可；本仓库随附固定提交的[许可原文](KohakuTerrarium.txt)，上游发行内容中的许可及声明也须保留。

## MagVarUpdate

- 项目来源：[MagicalAstrogy/MagVarUpdate](https://github.com/MagicalAstrogy/MagVarUpdate)
- 固定提交：`4a3645f19705e9e73bb4df3a8e731d5ff994ce80`
- 许可：**MIT**，原文见 [vendor/mvu/LICENSE](../vendor/mvu/LICENSE)。

`vendor/mvu/` 中的 `update_variables.ts`、`schema.ts` 和 `variable_def.ts` 是上述提交的未修改副本。构建时通过别名，将依赖 SillyTavern 宿主的导入替换为本项目的 [compat/upstream-host.ts](../compat/upstream-host.ts)。存储、初始化生命周期、隔离事件及原子提交由本项目实现。

## Tavern Helper / JS-Slash-Runner

- 接口参考：[n0vi028/JS-Slash-Runner](https://github.com/n0vi028/JS-Slash-Runner)
- 参考提交：`04a62b763513744b8cb3d85322f9b5c73bdf8a85`

本项目未包含或再分发该项目的实现代码。[compat/runtime.ts](../compat/runtime.ts) 是独立编写的接口适配层；参考其接口不代表具备完整兼容性。

## Python 依赖中的 GPL／AGPL 组件

以下组件由固定版本的 Kohaku Terrarium 引入，版本及依赖关系见 [uv.lock](../uv.lock)：

- **pathspec 1.1.1**：MPL-2.0，上游路径忽略规则匹配依赖；许可原文随该依赖发行包保留。
- **html2text 2025.4.15**：GPL-3.0-or-later，见[该版本许可](https://github.com/Alir3z4/html2text/blob/2025.4.15/COPYING)。上游网页读取工具直接导入并调用该库。
- **PyMuPDF 1.28.2**：AGPL-3.0 或另行取得的商业许可，见[官方许可说明](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright)。上游 PDF 读取工具通过 `fitz` 导入并调用。依赖被安装不表示本项目或用户已取得商业许可。

上述组件不能被本项目重新许可为仅限非商业用途。GPL／AGPL 对组合程序及分发的义务可能与本项目的非商业限制冲突，不能仅靠保留声明解决。这里记录来源与风险，不宣称本项目已完成兼容性确认；详见[许可审查与发行边界](../docs/development/licensing.md)。其他 Python 依赖仍适用各自许可。

## Office 文档解析

制卡 agent 使用 [Microsoft MarkItDown](https://github.com/microsoft/markitdown)（MIT）提供的 DOCX/XLSX 转换器。所选格式依赖包括 Mammoth（BSD-2-Clause）、lxml（BSD-3-Clause）、pandas（BSD-3-Clause）和 openpyxl（MIT），其余传递依赖沿用各自许可证，版本以 `uv.lock` 为准。

## Google GenAI SDK

Gemini 原生接入使用 Google 官方 [python-genai](https://github.com/googleapis/python-genai)（Python 包 `google-genai`，Apache-2.0）。准确版本及其 google-auth 等传递依赖以 [uv.lock](../uv.lock) 为准，许可原文随各依赖发行包保留。本项目通过其公开 API 调用生成、流式响应、模型目录与缓存对象，不包含修改后的 SDK 源码。

## 角色卡规范

本项目参考以下规范实现角色卡读写：

- [角色卡 V2 规范](https://github.com/malfoyslastname/character-card-spec-v2)
- [角色卡 V3 规范](https://github.com/kwaroran/character-card-spec-v3)

## 前端依赖

Vue、lodash、jQuery、Zod、YAML、JSON5、fast-json-patch、Acorn、DOMPurify、marked、mathjs 和 klona 通过 npm 安装。各依赖附带的许可文件以及构建产物中保留的声明继续适用；准确版本见 [package-lock.json](../package-lock.json)。

卡片离线模块打包使用 [esbuild](https://github.com/evanw/esbuild) 的 **MIT** 许可浏览器版本 `esbuild-wasm 0.25.10`。构建会将浏览器脚本、WebAssembly 二进制及其 `LICENSE.md` 原文复制至 `src/tavern/static/esbuild/`，随桌面和 Android 应用一同分发。

## 示例角色卡

[samples/](../samples/) 中的两张示例角色卡是为本项目制作的原创测试样例，当前版本随附的样例适用 [Terrarium Tavern 非商业许可 1.1](PROJECT_LICENSE.md)。使用、修改和再分发须注明 Terrarium Tavern 来源，并遵守非商业限制。此前已合法取得的样例所附授权不被追溯变更。

## Token 计数

[tiktoken](https://github.com/openai/tiktoken) 采用 **MIT** 许可，提供 BPE 分词实现以及 `o200k_base`、`cl100k_base` 词表。

经过校验的词表随项目包含在 [src/tavern/tokenizer_data/](../src/tavern/tokenizer_data/) 中，供离线计数使用。对于采用其他分词器的模型，这些词表仅提供参考估算，不代表服务商的实际计量结果。
