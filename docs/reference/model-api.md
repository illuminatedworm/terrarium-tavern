# 模型接入与参数映射

本文记录应用如何编译模型请求。表中的模型匹配规则和缓存档位是本项目的参数预设，不能用于推断任意中转的实际能力。用户配置步骤见[设置指南](../guides/settings.md)。

## 连接与协议

提供商保存认证方式、主域名、密钥或本地 ChatGPT 账号引用，以及是否使用 API 代理；模型分别保存提供商引用、模型名、接口协议与 API 路径。以下普通 API 规则适用于 `auth_mode=api_key`；订阅模式见下一节。

| `api_mode` | 默认 API 路径 | 生成端点 |
|---|---|---|
| `chat_completions` | `v1` | `/chat/completions` |
| `responses` | `v1` | `/responses` |
| `anthropic` | `v1` | `/messages` |
| `gemini` | `v1beta` | `/models/{model}:generateContent`；流式使用 `:streamGenerateContent` |

显式填写的 API 路径按原值拼接。模型列表从本地持久缓存读取，只有点击“更新模型列表”才请求提供商；缓存按提供商、接口地址、协议和密钥隔离，更新失败保留原列表。

请求使用提供商的密钥；缺少密钥时可从 `TAVERN_API_KEY` 读取。本地免密端点也需要非空占位值，例如 `local`。HTTP 客户端使用应用内 API 代理配置，不继承 shell 的代理环境变量。

## ChatGPT 订阅模式

`auth_mode=chatgpt_subscription` 通过独立账号服务取得访问令牌，固定使用 `https://api.openai.com/v1/responses`。不使用 `TAVERN_API_KEY`、自定义域名或 Codex backend-api。OAuth discovery、令牌交换、刷新和撤销均限制在已验证的 OpenAI 身份端点；HTTP 客户端不跟随携带凭据的重定向。

- 所有推理请求采用 `store:false`、`stream:true`。完整结果接口在内部消费 SSE，仅在收到 `response.completed` 后返回成功；中断、失败、额度错误和不完整响应均不提交本轮剧情。
- 按 `output_index` 汇集 `response.output_item.done` 的完整输出，与最终响应合并去重；完成事件中的空 `output` 不清除已接收的正文、工具调用和加密推理。仅收到未完成项时拒绝提交。
- system 消息原位转为 developer，保留完整原文和顺序；输入包含全部需要的历史，不发送 `previous_response_id`。
- 函数工具放入 `tavern` namespace，工具调用及结果保留命名空间；仅接收本轮允许的工具。指定单个工具时只暴露它，并使用 `tool_choice=required`。
- 请求体由订阅分支的允许字段构建，不发送普通采样、最大输出 token、显式缓存及其他通道自定义参数。缓存由服务端管理，仅使用稳定的 `prompt_cache_key`；不发送 `session-id`、`thread-id` 或 `x-codex-turn-state`。
- 原生回复归属由账号注册、授权会话代次和模型确定。正常刷新保持归属，重新授权／退出和切换账号会隔离旧的原生状态；操作内固定授权代次，不能中途切换到新登录。
- 模型目录使用相同账号的 OAuth 凭据请求 `/v1/models`，读取 `models` 中 `visibility=list` 的 `slug/display_name`，保留服务端顺序。缓存按账号和授权代次隔离。
- 目录附带 `supported_reasoning_levels`、`default_reasoning_level` 和接口不支持的 `unavailable_reasoning_levels`。订阅档位按模型能力过滤，`xhigh` 与 `max` 各自原样发送；当前直连接口不接受的 `ultra` 标为不可用。已载入目录时，“模型默认”发送目录声明的默认档位；未知能力时省略字段，由接口决定。

实现位于 `chatgpt/`，通过 `core/provider.py` 返回兼容 Terrarium 的 `ModelTransport` 子类；Writer、Tracker、Full Agent、Card Editor、脚本嵌套调用和探测共用同一入口。字段要求依据 [OpenAI 订阅通道限制](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)。后续章节的通用协议／缓存预设不覆盖订阅分支的限制。

## 请求身份

会话身份独立于缓存开关。Anthropic 使用 `X-Claude-Code-Session-Id` 与 `metadata.user_id` 中的设备／会话信息；Responses 使用 `session-id`、`thread-id` 和默认 `prompt_cache_key`。设备 ID 是数据库中随机生成的应用标识，account UUID 留空。

Half Agent 前台使用剧情会话 UUID，Tracker 前置、后置、重建和脚本调用按会话与用途派生身份。Full Agent 的失败续接保持同一会话身份，分支使用新身份；Card Editor 每个编辑对话有独立稳定身份。

Responses 的 `x-codex-turn-state` 只在同一逻辑操作内回传；新操作、重生成、模型、提供商或认证变化后清除。日志只记录其存在与摘要，不记录认证头。实现见 [request_context.py](../../src/tavern/scripting/request_context.py)，各模式的上下文记录层负责原生回复的持久化与失效判断。

## 思考强度

“默认”表示不发送思考控制字段。其他选项由 [reasoning.py](../../src/tavern/scripting/reasoning.py) 按协议及模型匹配规则编译。

| 接口 | 发送方式 |
|---|---|
| Chat Completions | `reasoning_effort` 或 `reasoning.effort`，由模型高级设置选择 |
| Responses | `reasoning.effort` |
| Anthropic Messages | 匹配 adaptive 的模型使用 `thinking.type=adaptive` 和 `output_config.effort` |
| Gemini | 匹配 Gemini 3 的模型使用 `generationConfig.thinkingConfig.thinkingLevel`；2.5 使用 `thinkingBudget` |

应用将 Opus 4.6/4.7/4.8/5/5.5、Sonnet 4.6/5/5.5 及已登记的 Fable/Mythos 名称归入 adaptive 路线。Opus 4.5 仅单独发送 effort；已知旧模型不能接受文字档位时，在请求前提示。未知中转别名采用标准字段，由服务商校验。

共用档位会发生映射：Claude 的 `minimal` 映射为 `low`；不支持 `xhigh` 而支持 `max` 的匹配型号映射为 `max`，Opus 4.5 映射为 `high`。Gemini 3 的 `xhigh` 映射为 `HIGH`，Pro 的 `minimal` 映射为 `LOW`。被标记为不能关闭思考的型号选择 `none` 时会报错。

模型映射还控制采样参数和强制工具选择。不能强制指定工具时，传输层使用 `auto` 并仅暴露目标工具；Full Agent 的换窗要求仍由运行时校验。维护模型匹配规则时，应同时核对参数编译与运行时权限，不只修改界面选项。

## 缓存时效

时效字段为每个模型的 `cache.retention`。`default` 或不适用的已保存值解析为当前协议与模型匹配规则中的最低档。下表是应用提供的选项及发送字段，单价不由应用定义。

| 匹配路径 | 可选档位 | 发送字段 |
|---|---|---|
| Claude Messages／Chat 兼容 | `5m`、`1h` | 内容块 `cache_control`；5m 省略 `ttl`，1h 显式设置 |
| GPT-5.6 及以后 Chat／Responses | `30m` | `prompt_cache_options.ttl` |
| GPT-5.5 Chat／Responses | `24h` | `prompt_cache_retention` |
| GPT-5、5.1、5.2、5.4、4.1 Chat／Responses | `in_memory`、`24h` | `prompt_cache_retention` |
| 其他匹配 GPT 及 o1/o3/o4 的路线 | `in_memory` | `prompt_cache_retention` |
| Gemini 原生／Google Chat 兼容显式对象 | `5m`、`15m`、`1h`、`24h` | 创建 `cachedContents` 时换算成秒字符串 `ttl` |

协议优先参与匹配。Claude 经 Responses 接入不会获得 Anthropic 内容块标记；Gemini 模型经 Anthropic Messages 接入时使用 Anthropic 档位。未知协议组合或不支持的自定义内容块格式不提供时效选项。

云端模式下，OpenAI 路线仍发送独立时效字段；Claude 原生仅在用户明确保存时效后发送顶层自动 `cache_control`。Claude Chat 兼容和 Gemini 隐式云端模式不发送时效。旧自定义 JSON 中的时效可以迁移到此选项，显式选择优先。

本地控制的协议标记、Gemini 对象生命周期及降级行为见[缓存设计](../design/cache-control.md)。跟进间隔属于会话，时效属于当前模式的正文模型；两者的保存范围不同。

## 原生回复与用量

传输层保留 Anthropic `content` 和 Responses `output` 的块顺序、工具 ID、思考签名及加密推理，用于兼容来源的续答。编辑、脚本改写或切换模型来源后是否回放，由对应上下文记录层判定；通用剧情历史始终保留。

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
- [test_model_api_modes.py](../../tests/backend/test_model_api_modes.py)、[test_unified_cache.py](../../tests/backend/test_unified_cache.py)：协议序列化和缓存行为。
