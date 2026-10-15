# 本地运行时缓存控制

本模块负责保持可复用请求内容、规划断点、保存已发送端点，以及管理缓存对象。适用于两种游玩模式和共享模型传输。操作步骤见[提示缓存指南](../guides/prompt-cache.md)，跟进建议的假设见[成本模型](cache-cost.md)。

## 分层与配置

| 层 | 职责 | 入口 |
|---|---|---|
| 上下文记录 | 保持已发送动态消息与原生回复顺序，变化时追加或重建 | [half_context.py](../../src/tavern/conversation/half_context.py)、[context_ledger.py](../../src/tavern/full_agent/context_ledger.py) |
| 断点计划 | 将会话策略解析为当前窗口中的剧情锚点 | [cache_breakpoint.py](../../src/tavern/conversation/cache_breakpoint.py) |
| 协议编译 | 模型预设、时效和字段映射 | [cache_policy.py](../../src/tavern/core/cache_policy.py)、[prompt_cache.py](../../src/tavern/scripting/prompt_cache.py) |
| 传输与记录 | 最终请求、已发送端点、缓存对象和降级 | [model_transport.py](../../src/tavern/scripting/model_transport.py)、[sent_breakpoints.py](../../src/tavern/scripting/sent_breakpoints.py)、[cloud_cache.py](../../src/tavern/scripting/cloud_cache.py) |

模型的 `cache.mode` 为 `cloud` 或 `runtime`。正文默认本地控制，后台与角色编辑默认云端。云端模式不注入本地断点、附加缓存 JSON 或 Gemini 对象引用；独立时效字段按[协议映射](../reference/model-api.md#缓存时效)处理。

跟进参数属于会话，模型与时效属于设置范围，实际发送过的端点属于传输记录。计划记录选择的位置，传输记录确认实际发送，服务商用量反映缓存读取与写入。

## 请求编译

1. 固定资料与工具定义保持稳定；历史和锚定世界书按剧情顺序排列。
2. 记忆、状态、授权要求及其他动态内容变化时，记录层追加新版本或撤销消息，保留已发送内容的位置。
3. 解析当前断点计划。传输层生成对应内容块标记或缓存对象引用，并完整发送后缀。
4. 记录最终请求边界、前缀及工具指纹。OpenAI 路线还重放实际发送过且仍匹配的端点。

编辑、换窗、固定资料或来源身份改变时，记录层重建适用的基线。请求仍包含当前完整上下文；保持前缀只提供复用条件，实际服务链路还可能受时效、路由和协议处理影响。

Full Agent 一轮中的工具续答不推进正文断点。Writer、Tracker 和角色卡编辑 agent 分别维护上下文，缓存评估按用途分组。

## 跟进状态与分支一致性

策略保存在 `sessions.cache_breakpoint_follow`：

| 字段 | 含义 |
|---|---|
| `interval` | 每新增多少个成功正文回合推进一次 |
| `offset` | 推进时选择倒数第几个已完成 AI 正文回复 |
| `anchor` | 当前固定的消息锚点 |
| `progress` | 自上次推进以来的新增正文计数 |

默认 `interval=2`、`offset=2`。新会话先使用固定前缀，达到首次跟进间隔后才选择正文锚点；已有会话保留已保存的策略。保存自动策略或恢复自动会立即定位并归零；之后计数只在新增正文提交的同一事务中推进。重生成、维护、失败、取消和重复提交不增加计数。

`PUT /api/sessions/{sid}/cache-breakpoint` 接受 `expected_revision`，以及可选的正整数 `follow_interval` 和 `reply_offset`；省略参数保留已有值。界面还可选择手动固定。开场白与工具交换不能作为目标。

分支继承策略。自动模式在新分支重新定位并归零，手动位置映射到继承的消息；目标失效时保留选择并提示。目标不在当前分支或实际窗口时，使用可用固定前缀，不擅自换成另一条正文。

## 协议适配

| 接口 | 本地控制行为 |
|---|---|
| Chat Completions | 使用模型预设或自定义内容块标记；Google 兼容扩展可引用 Gemini 对象 |
| Responses | 受支持的匹配路线发送 `prompt_cache_breakpoint` 和 `prompt_cache_options` |
| Anthropic Messages | 发送原生 `cache_control` 内容块标记 |
| Gemini GenerateContent | 创建 `cachedContents`，生成时引用 `cachedContent` 并发送后缀 |

自动匹配是代码预设，不探测中转能力。更早的 GPT/o 系列按匹配规则使用隐式策略和保留参数；未知模型需要自定义格式，否则降级云端。自定义字段不能覆盖模型、消息、工具、采样或控制字段。

原生接口只把固定前缀编为顶层系统指令，动态上下文保留对应历史位置。工具权限、变量更新和强制换窗仍由应用执行校验。

## OpenAI 已发送端点

使用 `prompt_cache_breakpoint` 时，当前计划位置仅表示本次选择的新端点。请求同时携带此前实际发送、内容前缀仍匹配的全部端点，应用不按数量上限裁剪记录；服务端如何查找和写入不由本地标记数量决定。

只有 HTTP 请求体发出才保存发送事实。后续响应超时、取消、解析失败或正文未提交不撤销记录；仅预览、修改计划或连接建立失败不会新增记录。

记录位于 `sent_cache_breakpoints`，按会话／用途、接口地址、凭据和模型隔离。匹配使用最终协议内容块的前缀指纹，排除标记字段本身。跨轮、重启和备份恢复保留；分支复制后仅重放仍与实际上下文匹配的端点。编辑或换窗后按内容指纹重新匹配端点。

手动前移计划位置不会撤销仍匹配的较后标记。没有选出新端点时，也可以重放已有记录。云端、降级请求和其他标记协议不注入这些记录。最终请求诊断中的 `cache.sent_breakpoints` 是实际携带的标记，`planned_index` 是本次计划位置。

记录随所属会话或编辑工作区删除，不受普通工作日志清理影响。

## Gemini 对象生命周期

对象包含断点之前的系统规则、消息、工具定义及工具选择配置。每个连接、凭据、协议、模型和会话只保留一个活动槽；前缀、窗口、时效变化或对象过期时重建，不向旧对象追加内容。

默认时效为 300 秒，可按模型档位调整。有效期取所选时长与服务商 `expireTime` 中较早者，临近过期时停止复用。替换后尽力删除旧对象；删除失败或进程退出后依赖 TTL 回收。对象句柄不进入剧情快照。

前缀不足本地估算门槛、创建失败或接口不存在时，完整发送上下文并记录降级。断点位于无法拆分的固定系统指令内部时，提示选择更后的正文，不扩大用户选择的缓存范围。

## 失败与观察

对象失效或服务商明确拒绝缓存字段时，最多重发一次完整请求。超时、断流、普通参数错误及服务端错误不自动重发生成。确定的不支持结果按配置指纹记忆，配置变化、重新检查连接或重启后允许再次探测。

工作记录保存实际 URL、最终请求、计划与发送状态、工具定义和前缀摘要。读取／写入计数缺失保持未知；原始 usage 独立保存，框架累计视图不覆盖这些未知值。字段口径见[模型接入参考](../reference/model-api.md#原生回复与用量)。

## 验证入口

| 测试 | 保护的行为 |
|---|---|
| [test_cache_follow.py](../../tests/backend/test_cache_follow.py)、[test_session_cache_breakpoint.py](../../tests/backend/test_session_cache_breakpoint.py) | 跟进计数、手动位置、分支和窗口 |
| [test_unified_cache.py](../../tests/backend/test_unified_cache.py) | 四协议请求、Gemini 对象及失败降级 |
| [test_sent_cache_breakpoints.py](../../tests/backend/test_sent_cache_breakpoints.py) | 发送事实、持久化、内容匹配及失败路径 |
| [test_full_agent_alignment.py](../../tests/backend/test_full_agent_alignment.py)、[test_half_agent_alignment.py](../../tests/backend/test_half_agent_alignment.py) | 动态内容追加、原生回复和预览一致性 |

真实请求审计见[缓存链路评估](../development/validation.md#缓存链路评估)，合成协议探测工具见[工具说明](../../tools/probe_prefix_cache.md)。
