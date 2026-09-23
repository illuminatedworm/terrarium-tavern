# 第三方声明

本文记录本项目使用或参考的第三方组件、规范及其来源。各组件适用各自的许可，具体条款以相应许可原文为准。

## Kohaku Terrarium

- 项目来源：[Kohaku-Lab/KohakuTerrarium](https://github.com/Kohaku-Lab/KohakuTerrarium)
- 固定提交：`c01854de42eacf280d09b2dcdaeae51bab28d5ef`
- 版权所有：2024–2026 Shih-Ying Yeh（KohakuBlueLeaf）及贡献者。
- 许可名称：**KohakuTerrarium License 1.0**。

本项目将 Kohaku Terrarium 作为依赖安装，并在应用名称与可见署名中保留 Terrarium 标识。许可原文包含在上游发行内容中。

## MagVarUpdate

- 项目来源：[MagicalAstrogy/MagVarUpdate](https://github.com/MagicalAstrogy/MagVarUpdate)
- 固定提交：`4a3645f19705e9e73bb4df3a8e731d5ff994ce80`
- 许可：**MIT**，原文见 [vendor/mvu/LICENSE](vendor/mvu/LICENSE)。

`vendor/mvu/` 中的 `update_variables.ts`、`schema.ts` 和 `variable_def.ts` 是上述提交的未修改副本。构建时通过别名，将依赖 SillyTavern 宿主的导入替换为本项目的 [compat/upstream-host.ts](compat/upstream-host.ts)。存储、初始化生命周期、隔离事件及原子提交由本项目实现。

## Tavern Helper / JS-Slash-Runner

- 接口参考：[n0vi028/JS-Slash-Runner](https://github.com/n0vi028/JS-Slash-Runner)
- 参考提交：`04a62b763513744b8cb3d85322f9b5c73bdf8a85`

本项目未包含或再分发该项目的实现代码。[compat/runtime.ts](compat/runtime.ts) 是独立编写的接口适配层；参考其接口不代表具备完整兼容性。

## 角色卡规范

本项目参考以下规范实现角色卡读写：

- [角色卡 V2 规范](https://github.com/malfoyslastname/character-card-spec-v2)
- [角色卡 V3 规范](https://github.com/kwaroran/character-card-spec-v3)

## 前端依赖

Vue、lodash、jQuery、Zod、YAML、JSON5、fast-json-patch、Acorn、DOMPurify、marked、mathjs 和 klona 通过 npm 安装。各依赖附带的许可文件以及构建产物中保留的声明继续适用；准确版本见 [package-lock.json](package-lock.json)。

## 示例角色卡

[samples/](samples/) 中的两张示例角色卡是为本项目制作的原创测试样例。注明 Terrarium Tavern 来源后，可以使用、修改和再分发。

## Token 计数

[tiktoken](https://github.com/openai/tiktoken) 采用 **MIT** 许可，提供 BPE 分词实现以及 `o200k_base`、`cl100k_base` 词表。

经过校验的词表随项目包含在 [src/tavern/tokenizer_data/](src/tavern/tokenizer_data/) 中，供离线计数使用。对于采用其他分词器的模型，这些词表仅提供参考估算，不代表服务商的实际计量结果。
