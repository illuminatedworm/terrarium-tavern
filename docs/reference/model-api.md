# 模型接入与参数映射

模型请求由应用按提供商、协议和模型配置编译。下表列出本项目的参数预设，接入服务的实际能力需通过连接与请求结果确认。配置步骤见[设置指南](../guides/settings.md)。

## 连接与协议

提供商保存认证方式、主域名、密钥或本地 ChatGPT 账号引用，以及是否使用 API 代理；模型分别保存提供商引用、模型名、接口协议与 API 路径。以下普通 API 规则适用于 `auth_mode=api_key`；订阅模式见下一节。

| `api_mode` | 默认 API 路径 | 生成端点 |
|---|---|---|
| `chat_completions` | `v1` | `/chat/completions` |
| `responses` | `v1` | `/responses` |
| `anthropic` | `v1` | `/messages` |
| `gemini` | `v1beta` | `/models/{model}:generateContent`；流式使用 `:streamGenerateContent` |

显式填写的 API 路径按原值拼接。模型列表从本地持久缓存读取，只有点击“更新模型列表”或模型选单内的“获取模型列表”才请求提供商；缓存按提供商、接口地址、协议和密钥隔离，更新失败保留原列表。

`gemini` 在界面显示为“Google GenAI · Gemini”，通过官方 Python `google-genai` SDK 调用 GenerateContent、流式生成、模型列表及显式缓存对象 API；配置键仍为 `gemini`，现有配置无需改名。使用 Gemini Developer API 的 API Key 方式，可保留自定义服务地址和路径，不自动切换到 Vertex AI／Enterprise 服务。SDK 自动工具执行、自动续写和自动重试均关闭，工具权限与续答仍由 Roleplay Runtime 调度；只有明确拒绝缓存对象时允许一次完整上下文重发。请求日志记录 SDK 最终序列化的 HTTP 正文，而非调用前的估算结构。

流式解析兼容中转在完整 JSON 数据与下一个命名事件之间遗漏分隔空行的响应，保留正常多行 JSON。Gemini 流中的错误事件统一作为请求失败处理，原始响应可通过可选的失败请求 Debug 记录查看；即使 HTTP 为 200 或已收到部分正文，后续错误仍使整轮失败，不提交未完成内容。

Chat Completions 完成后按实际内容判断中转追加的尾帧：允许 `delta` 缺失、为 `null`、空对象，或包含空字符串、`null`、空数组及角色等元数据；候选编号等元数据不算内容，工具占位项没有非空 ID、函数名或参数时也视为空。后续结束原因可为空、缺失或任一受支持值，但不覆盖首次结束原因；用量帧仍正常计入。完成后实际追加正文、拒绝文本、思考、推理详情或工具 ID／函数名／参数，以及后续错误事件或无效结束原因仍使本轮失败。

普通 API 的 HTTP 失败会在回合错误与后台工作记录中保留状态码、服务端错误码及说明，不依赖开启失败请求 Debug。JSON 响应读取 `error` 对象／字符串或顶层的错误说明；非 JSON 响应显示文本。错误说明先脱敏当前请求密钥和 Bearer 凭据，最多显示 4000 字符，超出时标明为片段；后台错误行的悬停提示保留该说明，完整 HTTP 原始证据仍按失败请求 Debug 设置保存。

请求使用提供商的密钥；缺少密钥时可从 `TAVERN_API_KEY` 读取。本地免密端点也需要非空占位值，例如 `local`。HTTP 客户端使用应用内 API 代理配置，不继承 shell 的代理环境变量。

Chat Completions 的非流式响应同样必须提供有效的 `finish_reason`；HTTP 200 但结束原因缺失、为空或不受支持时，整轮失败，不把部分正文或工具调用当成完整回复提交。HTTP 200 流中的错误说明也会先脱敏实际请求密钥和 Bearer 凭据，再限量显示及记录，不依赖开启调试。

## ChatGPT 订阅模式

`auth_mode=chatgpt_subscription` 通过独立账号服务取得访问令牌，固定使用 `https://api.openai.com/v1/responses`。不使用 `TAVERN_API_KEY`、自定义域名或 Codex backend-api。OAuth discovery、令牌交换、刷新和撤销均限制在已验证的 OpenAI 身份端点；HTTP 客户端不跟随携带凭据的重定向。

- 所有推理请求采用 `store:false`、`stream:true`。完整结果接口在内部消费 SSE，仅在收到 `response.completed` 后返回成功；中断、失败、额度错误和不完整响应均不提交本轮剧情。
- 按 `output_index` 汇集 `response.output_item.done` 的完整输出，与最终响应合并去重；完成事件中的空 `output` 不清除已接收的正文、工具调用和加密推理。仅收到未完成项时拒绝提交。
- system 消息原位转为 developer，保留完整原文和顺序；输入包含全部需要的历史，不发送 `previous_response_id`。
- 函数工具放入 `tavern` namespace，工具调用及结果保留命名空间；仅接收本轮允许的工具。指定单个工具时只暴露它，并使用 `tool_choice=required`。
- 请求体由订阅分支的允许字段构建，不发送普通采样、最大输出 token、显式缓存及其他通道自定义参数。缓存由服务端管理，仅使用稳定的 `prompt_cache_key`；不发送 `session-id`、`thread-id` 或 `x-codex-turn-state`。
- 官方 Full Agent 选择云端 compact 后，用户发起回合时由运行时检查编译后的输入长度；开启自动压缩且超过设置的窗口上限时，先通过独立整理请求发送 `context_management` 与 `compact_threshold`，再继续本轮。正文与工具续答不携带自动压缩配置。支持完整加密项续接；独立 `/responses/compact` 不用于此 OAuth 通道。双向模式转换从完整原文按整轮交流重放；切回本地且需要换窗时使用专用的 `prepare_handoff` 结构化调用，验证后再更新本地状态，不执行剧情工具。
- 原生回复归属由账号注册、授权会话代次和模型确定。正常刷新保持归属，重新授权／退出和切换账号会隔离旧的原生状态；操作内固定授权代次，不能中途切换到新登录。
- 模型目录使用相同账号的 OAuth 凭据请求 `/v1/models`，读取 `models` 中 `visibility=list` 的 `slug/display_name`，保留服务端顺序。缓存按账号和授权代次隔离。
- 目录附带 `supported_reasoning_levels`、`default_reasoning_level` 和接口不支持的 `unavailable_reasoning_levels`。订阅档位按模型能力过滤，`xhigh` 与 `max` 各自原样发送；当前直连接口不接受的 `ultra` 标为不可用。已载入目录时，“模型默认”发送目录声明的默认档位；未知能力时省略字段，由接口决定。

实现位于 `chatgpt/`，通过 `core/provider.py` 返回兼容 Terrarium 的 `ModelTransport` 子类；Writer、Tracker、Full Agent、参谋 agent、制卡 agent、脚本嵌套调用和探测共用同一入口。字段要求依据 [OpenAI 订阅通道限制](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)。后续章节的通用协议／缓存预设不覆盖订阅分支的限制。

## 请求身份

会话身份独立于缓存开关。Anthropic 使用 `X-Claude-Code-Session-Id` 与 `metadata.user_id` 中的设备／会话信息；Responses 使用 `session-id`、`thread-id` 和默认 `prompt_cache_key`。设备 ID 是数据库中随机生成的应用标识，account UUID 留空。

Half Agent 前台使用剧情会话 UUID，Tracker 前置、后置、重建和脚本调用按会话与用途派生身份。Full Agent 的失败续接保持同一会话身份，分支使用新身份；制卡 agent 每个编辑对话有独立稳定身份。

Responses 的 `x-codex-turn-state` 只在同一逻辑操作内回传；新操作、重生成、模型、提供商或认证变化后清除。日志只记录其存在与摘要，不记录认证头。实现见 [request_context.py](../../src/tavern/scripting/request_context.py)，各模式的上下文记录层负责原生回复的持久化与失效判断。

## 思考强度

“默认”表示不发送思考控制字段。其他选项由 [reasoning.py](../../src/tavern/scripting/reasoning.py) 按协议及模型匹配规则编译。

普通 API 的设置页与输入框从同一套后端规则读取可选档位，切换接口或模型后立即重新匹配。已选但不适用的档位保留显示并禁用，提示重选，不自动改写设置。规则只在本机读取，不向服务商请求能力目录。

| 接口 | 可选档位（均另含模型默认） | 发送方式 |
|---|---|---|
| Chat Completions | none、minimal、low、medium、high、xhigh、max；Gemini 名称按 Gemini 兼容规则收窄 | `reasoning_effort` 或 `reasoning.effort`，由模型高级设置选择 |
| Responses | none、minimal、low、medium、high、xhigh、max | `reasoning.effort` |
| Anthropic Messages | low、medium、high；匹配 adaptive 的型号另有 max，支持时另有 xhigh；可关闭的型号另有 none | 匹配 adaptive 的模型使用 `thinking.type=adaptive` 和 `output_config.effort` |
| Gemini 3／4 | 按型号提供 minimal、low、medium、high 的子集；3.8/3.7 Flash 和 4 仅 low、medium、high | `generationConfig.thinkingConfig.thinkingLevel` |
| Gemini 2.5 原生 | minimal=128、low=1024、medium=4096、high=8192、xhigh=16384 思考 tokens；支持关闭时另有 none=0；Flash-Lite 不提供 minimal | `generationConfig.thinkingConfig.thinkingBudget`，界面显示对应预算 |
| Gemini 2.5 Chat 兼容 | low、medium、high；支持关闭时另有 none | 使用 Chat 的思考字段，由服务商将文字档位转换为预算 |

OpenAI 两种协议提供协议支持的完整集合，具体模型与中转支持范围仍由服务商决定；不会仅凭非 OpenAI 名称将 Responses 切为其他协议。已知旧 Claude 不提供文字档位，仍可选择默认或其支持的关闭思考。未知 Anthropic 中转别名提供 low、medium、high 与关闭思考，由服务商校验。

应用将 Opus 4.6/4.7/4.8/5/5.5、Sonnet 4.6/5/5.5 及已登记的 Fable/Mythos 名称归入 adaptive 路线。Opus 4.5 仅单独发送 effort；已知旧模型不能接受文字档位时，在请求前提示。未知中转别名采用标准字段，由服务商校验。

旧配置中的兼容映射保留：Claude 的 `minimal` 映射为 `low`；不支持 `xhigh` 而支持 `max` 的匹配型号映射为 `max`，Opus 4.5 映射为 `high`。Gemini 3 的 `xhigh` 映射为 `HIGH`，Pro 的 `minimal` 映射为 `LOW`。新配置使用表中的规范档位，旧值在请求时转换。被标记为不能关闭思考的型号选择 `none` 时会报错；Gemini 原生不支持的档位会在请求前提示重选。

协议依据见 [OpenAI reasoning](https://developers.openai.com/api/docs/guides/reasoning)、[Claude effort](https://platform.claude.com/docs/en/build-with-claude/effort)、[Gemini GenerateContent thinking](https://ai.google.dev/gemini-api/docs/generate-content/thinking) 与 [Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai.md)。预算数值为本项目预设。

模型映射还控制采样参数和强制工具选择。不能强制指定工具时，传输层使用 `auto` 并仅暴露目标工具；Full Agent 的换窗要求仍由运行时校验。维护模型匹配规则时，应同时核对参数编译与运行时权限，不只修改界面选项。

Gemini 3.6 Flash、3.5 Flash-Lite 及后续型号已废弃 `temperature`、`top_p`、`top_k`：原生和 Chat 兼容配置隐藏这些控件，请求层也不发送已保存值或单次调用覆盖值，并在界面说明由服务端控制采样。较早型号保留可用控件；未知中转别名不凭猜测关闭参数。SDK 对部分嵌套消息字段使用等价的 protobuf snake_case 名称，实际发送形式以请求日志为准。依据：[Google 参数迁移说明](https://ai.google.dev/gemini-api/docs/whats-new-gemini-3.6)。

## 缓存时效

时效字段为每个模型的 `cache.retention`。`default` 或不适用的已保存值解析为当前协议与模型匹配规则中的最低档。下表是应用提供的选项及发送字段，单价不由应用定义。

| 匹配路径 | 可选档位 | 发送字段 |
|---|---|---|
| Claude Messages／Chat 兼容 | `5m`、`1h` | 内容块 `cache_control`；5m 省略 `ttl`，1h 显式设置 |
| GPT-5.6 及以后 Chat／Responses | `30m` | `prompt_cache_options.ttl` |
| GPT-5.5 Chat／Responses | `24h` | `prompt_cache_retention` |
| GPT-5、5.1、5.2、5.4、4.1 Chat／Responses | `in_memory`、`24h` | `prompt_cache_retention` |
| 其他匹配 GPT 及 o1/o3/o4 的路线 | `in_memory` | `prompt_cache_retention` |
| 支持显式缓存的 Gemini 2.5／3 原生或 Google Chat 兼容 | `5m`、`15m`、`1h`、`24h` | 创建 `cachedContents` 时换算成秒字符串 `ttl` |
| Gemini 4 及后续 | 无本地时效档位 | 仅服务端隐式缓存，不创建或引用显式对象 |

协议优先参与匹配。Claude 经 Responses 接入不会获得 Anthropic 内容块标记；Gemini 模型经 Anthropic Messages 接入时使用 Anthropic 档位。未知协议组合或不支持的自定义内容块格式不提供时效选项。

云端模式下，OpenAI 路线仍发送独立时效字段；Claude 原生仅在用户明确保存时效后发送顶层自动 `cache_control`。Claude Chat 兼容和 Gemini 隐式云端模式不发送时效。旧自定义 JSON 中的时效可以迁移到此选项，显式选择优先。

Google GenAI 原生模式不显示自定义断点字段、JSON 值与附加缓存 JSON，也不发送这些已保存值。Gemini 4 及后续还隐藏本地缓存控制、断点和时效入口；完整上下文仍按稳定前缀顺序发送。原配置保留以便切换回支持的协议或型号，界面明确标明哪些值不适用，不重写用户数据。依据：[Google 缓存说明](https://ai.google.dev/gemini-api/docs/caching)。

本地控制的协议标记、Gemini 对象生命周期及降级行为见[缓存设计](../design/cache-control.md)。跟进间隔属于会话，时效属于当前模式的正文模型；两者的保存范围不同。

## 原生回复与用量

传输层保留 Anthropic `content` 和 Responses `output` 的块顺序、工具 ID、思考签名及加密推理，用于兼容来源的续答。编辑、脚本改写或切换模型来源后是否回放，由对应上下文记录层判定；通用剧情历史始终保留。

Full Agent、Tracker、记忆历史补查和 制卡 agent 在生成工具结果时记录执行失败状态，Anthropic 续答将其映射为 `tool_result.is_error=true`；保留的工具记录经过上下文投影与回放时不丢失此状态。参数错误、权限拒绝、补查额度耗尽，以及制卡中因用户补充要求而取消的未执行调用均属于失败；正常搜索零命中、重复查询提示，以及读取到含 `error` 或 `ok:false` 字段的业务数据不据此判为失败。Chat Completions、Responses 和 Gemini 保留错误正文，不发送 Anthropic 专属字段。Writer 与普通脚本生成不执行工具循环。

应用保存原始 `usage`，并提供统一显示字段。缓存读取或写入字段缺失表示未知，不能显示为零命中。

| 接口 | 输入和输出口径 |
|---|---|
| Anthropic | 输入合计包含未缓存输入、缓存读取与创建 |
| Gemini | 输出合计包含候选文本和思考量；缓存读取取 `cachedContentTokenCount` |
| Chat／Responses | 读取原生输入输出字段，兼容已支持的缓存读取／创建别名 |

实际费用需要结合服务商账单；本地 token 估算只用于编排与提示，见[Token 计数](../guides/token-counting.md)。

## 实现入口

- [core/cache_policy.py](../../src/tavern/core/cache_policy.py)：端点拼接、缓存预设、时效及用量归一化。
- [core/provider.py](../../src/tavern/core/provider.py)：密钥、代理和 HTTP 客户端。
- [scripting/model_transport.py](../../src/tavern/scripting/model_transport.py)、[model_stream.py](../../src/tavern/scripting/model_stream.py)：最终请求、流式解析与降级。
- [scripting/genai_transport.py](../../src/tavern/scripting/genai_transport.py)：Google 官方 SDK、操作级 HTTP 记录与取消时资源回收。
- [test_model_api_modes.py](../../tests/backend/test_model_api_modes.py)、[test_unified_cache.py](../../tests/backend/test_unified_cache.py)：协议序列化和缓存行为。
