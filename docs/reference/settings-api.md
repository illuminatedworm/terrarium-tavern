# 设置持久化与 API

设置按范围保存，并通过修订号处理并发修改。当前设置格式为 12、数据库 schema 为 13、个人数据包格式为 5。旧设置和旧包中的非对话资料可以导入；旧剧情对话不兼容整轮重试格式，读取或导入时明确拒绝。界面操作见[设置指南](../guides/settings.md)，模型传输字段见[模型接入参考](model-api.md)。

`shared.kemini` 属于 `shared.misc` 页面，包含默认关闭的 `enabled`、`normal_disclaimer_enabled` 及十三项可编辑文本。`enabled` 允许创建并继续 Kemini 实验对话，不直接改变任何对话；为 `false` 时 `POST /api/sessions` 拒绝 `kemini: true`，已有 Kemini 实验对话的生成回合被拒绝。免责声明在 Kemini 实验对话中且 `normal_disclaimer_enabled` 为 `true` 时生效；主开关关闭时保留其保存值，界面将其置灰。复用共享设置 PATCH、修订检查和页面撤回。

会话的 `kemini` 为创建时固定的布尔值（`POST /api/sessions` 可选字段，默认 `false`），仅允许 `channel_origin` 为 `standard`；会话更新接口不能修改，分支与数据包原样继承，导入时校验同样约束。剧情消息的 `auxiliary` 为 `{kind: thinking|disclaimer|extra, text: string}[]`，旧消息为空列表；`kemini_auxiliary` SSE 事件的 `items` 替换当前响应辅助记录，沿用事件序号去重。

`thinking_prompt` 和 `disclaimer_text` 保存完整提示及其格式要求，Runtime 不另加自拟包装。默认值来自原预设对应条目，移除 SillyTavern 宏，并将独立边界标签与 Interleaving 包装改为 Markdown 分隔。新增的 roleplay_prompt 位于助手确认前，content_prompt 位于自检后，output_prompt 位于传输指令后；均可显式保存。请求只读取已保存文本；设置升级仅在旧值完全匹配旧默认值时更新明确列出的默认提示，自定义文本不变。

参谋 agent 配置位于 `shared.advisor`，设置文档为版本 10。`model` 与 `official_model` 分别引用同渠道供应商；`system_prompt` 进入共享提示词目录并支持显式保存。故事级覆盖、默认范围、工具与接口见[参谋 agent 设计](../design/advisor.md)。参谋 agent 存储引入数据库 schema 4，旧数据库在迁移前备份。

## ChatGPT 账号接口

提供商新增 `auth_mode: api_key | chatgpt_subscription`（缺省为 `api_key`）及可空的 `chatgpt_account_id`。设置文档为版本 10；账号引用可以随设置迁移，但账号元数据、Client ID、host ID 和令牌不进入设置数据库及导出包。没有本地注册记录的引用显示需要重新连接。

以下接口受本地访问／跨来源边界保护。账号操作立即生效，不属于设置修订或撤回。

| 接口 | 作用 |
|---|---|
| `GET /api/auth/chatgpt/accounts` | 返回账号 `id/label/email/status/plan_enabled/generation`、首次用量提示状态和管理用量链接 |
| `POST /api/auth/chatgpt/login` | 接收 `provider_id`、可空 `account_id` 和 `enable_plan`；后端打开系统浏览器，返回事务摘要 |
| `GET /api/auth/chatgpt/login` | 当前事务摘要，或 `status=idle` |
| `GET /api/auth/chatgpt/login/{id}` | 查询 `pending/exchanging/completed/cancelled/expired/failed`、账号引用及安全错误信息 |
| `DELETE /api/auth/chatgpt/login/{id}` | 取消待完成的登录 |
| `POST /api/auth/chatgpt/accounts/{id}/logout` | 可传 `provider_id` 选择代理选项；停止请求、清除本地令牌，返回 `revocation_confirmed` |
| `DELETE /api/auth/chatgpt/accounts/{id}` | 退出并删除未被提供商引用的注册；被引用时拒绝删除 |
| `POST /api/auth/chatgpt/welcome` | 保存首次订阅用量说明已确认状态 |

API 不返回授权 URL、state、nonce、PKCE verifier 或令牌。OAuth 回调由独立的 `127.0.0.1` 临时监听器处理，路径固定为 `/auth/callback`。`generation` 是授权会话的非秘密版本，仅用于账号切换后刷新客户端缓存。

模型列表接口保持 `models: string[]`，订阅模式额外返回 `model_details`：每项包含 `id/display_name`，目录提供能力时还包含 `supported_reasoning_levels: [{effort, description}]`、`default_reasoning_level` 和 `unavailable_reasoning_levels: [{effort, reason}]`。支持档位为账号目录与当前订阅接口契约的交集，保留目录顺序。

可操作的 ChatGPT 错误响应包含 `detail/code/upstream_status/request_id/usage_url`；不能从额度错误自行推算套餐重置时间，也不能回退至其他计费凭据。

## 配置范围与修订号

数据库中的设置文档版本为 12，由三个独立范围组成：

| 范围 | 主要内容 | 模型字段 |
|---|---|---|
| `full_agent` | 模型、工具与记忆、检索、提示词及默认修改保存方式 | `model`、`official_model` |
| `half_agent` | 模型、历史与记忆、检索、提示词、上下文配方及默认修改保存方式 | `foreground`、`background`、`official_foreground`、`official_background` |
| `shared` | 提供商、参谋 agent、制卡 agent、联网服务、本地模型、偏好及清理参数 | `card_author`、`advisor.model`、`advisor.official_model` |

每个模型配置的 `provider_id` 指向当前提供商，平铺的 `api_mode`、`api_path`、`model` 是当前生效值。`provider_connections` 按提供商 ID 保存其他提供商的这三个字段，不能包含当前提供商，否则以重复连接配置报错，防止历史值覆盖可见编辑。切换时将原提供商的当前值移入记录，再取出目标提供商的记录作为当前值；无记录时使用空模型、空路径与 `chat_completions`（官方渠道固定为 Responses）。该映射整体 PATCH，例如 `model.provider_connections`，与当前提供商及三个字段在一次请求中保存，支持页面撤回和设置导入导出。

游玩、制卡和参谋 agent 的对话级 `model_selection` 包含当前 `provider_id/model`，以及可选的 `provider_models`，保存其他提供商在此对话中上次选择的模型名称；由服务器在切换时更新，不改变全局模型名称。对话选择的协议和路径通过相应角色的 `provider_connections` 查找，不从另一提供商继承。游玩和制卡允许 `model: ""` 表示所选提供商尚未配置模型；此时禁止发送。现有对话与分支继承这些记录。

两种模式的 `policy` 仅控制保存：`automatic` 自动保存，`review` 在实际变量、角色设定或世界书有变化时审核整轮。记忆和运行记录不触发审核。旧 `directed`／`evolving` 读取为 `automatic`，旧 `preview` 读取为 `review`；会话级 `policy: null` 沿用全局。设定写入只由请求的 `allow_content_edit` 授权，变量任务由 `update_variables` 开启，授权与保存方式分别校验。

`PUT /api/sessions/{sid}/options` 接受 `expected_revision` 及需修改的会话字段，省略的字段保持原值。`update_variables` 是对话级布尔偏好，默认 `false`，由输入框勾选时立即保存，分支和个人数据包保留；回合请求仍显式传入当轮的 `update_variables`。偏好列由 schema 5 引入，升级前的数据库备份见[数据库与设置迁移](#数据库与设置迁移)，不从历史回合推断用户选择。

每个范围有自己的 `revisions` 值。更新共享提供商可能使其他范围中的模型能力检查失效，因此一次响应可能包含多个范围的新修订号。客户端应合并响应中的实际值，不能只递增提交范围的本地计数。

`SettingsDocument.runtime(mode)` 生成一次操作的深拷贝配置。正在执行的回合继续使用开始时的快照；保存后的配置用于后续操作。Full Agent 与 制卡 agent 都有明确模型配置，不在运行时继承 Half Agent 模型。

`full_agent.tools.parallel_tool_calls` 为严格布尔值，默认 `true`；旧设置缺少此字段时自动采用开启值。它对应“工具与记忆”页的“单次允许多个工具调用”，只控制 Full Agent；通过 `PATCH /api/settings/full_agent` 的 `changes` 传入 `{"tools.parallel_tool_calls":false}` 可关闭。

## 读取与写入

Chat Completions 流式响应兼容在结束后追加 `delta: {}` 的重复收尾包（结束原因为 `stop` 或与首次相同），保留首次结束原因与已收集的工具调用，并继续接收用量统计；结束后新增正文、推理或工具参数仍会报错，不提交回合。

`shared.model_request_mode` 可取 `auto`（默认，保持各调用方原有方式）、`stream`（普通 API 统一使用流式传输）、`non_stream`（普通 API 统一使用非流式传输）。通过 `PATCH /api/settings/shared` 的 `changes` 写入 `{"model_request_mode":"stream"}`；它属于 `shared.web` 设置页面（“联网与代理”），支持该页撤回。配置进入每次操作的运行时快照，覆盖各角色的模型生成请求；ChatGPT 订阅通道始终流式，独立的 compact、向量、目录和缓存管理端点使用各自请求。

`shared.debug_failed_requests` 为严格布尔值，默认 `false`，属于 `shared.misc` 页，支持自动保存、页面撤回和个人设置导入导出，从下一次操作生效。开启后保留失败模型生成请求的原始 HTTP 证据，成功请求不落盘。`GET /api/debug/failed-requests?offset=0` 按时间倒序返回每页最多 100 条的 HTTP 信息及记录总数；`offset` 是从 0 开始的记录偏移条数，默认 0、下界包含 0，无固定上界，越过末尾返回空列表。`GET /api/debug/failed-requests/{UUID}` 下载对应 ZIP，不提供任意路径访问。记录格式、正文限额、脱敏及清理规则见[杂项设置](../guides/settings.md#杂项)。

`shared.debug_full_agent_prefix` 为严格布尔值，默认 `false`，属于 `shared.misc` 页，支持自动保存、页面撤回和个人设置导入导出，从下一次 Full Agent 操作生效。仅在运行时内存保留最近两次 SDK 序列化后的生成请求正文。`full_agent_prefix_debug` 回合事件的 `payload.message` 用于现有通知：消息参数（`messages`／`input`／`contents`）一致或数组正常追加时显示“公共前缀与首个差异未错位”，不一致时显示“公共前缀或首个差异发生错位”；其他参数只显示“一致”或“不一致”，不展示具体字节信息。`payload.parameters` 保留诊断数据，按实际顶层 JSON 参数分别返回 `parameter`、`previous_bytes`、`current_bytes`、`status`；同时存在的参数还返回 `common_prefix_bytes` 和 `first_difference_byte`（从 0 开始，完全相同时为 `null`）。`status` 为 `identical`、`array_extended`、`changed`、`added` 或 `removed`；不存在一侧的字节数为 `null`。首次请求只通知已建立基线，参数结果为空。结果不包含参数原文。比较边界与暂存清理见[杂项设置](../guides/settings.md#杂项)。

Full Agent 流式接收时，只把正文工具的 content 参数通过 `text` 事件追加到未保存预览；普通过程说明不进入预览。`text_reset` 以 `payload.text` 替换预览，用于清空被拒绝的正文提案、按完整结果校正或显示脚本处理后的正文。已接收的正文在后续工具请求期间保留。客户端必须按事件序号去重，完整重放时从空文本开始；预览快照与对应事件原子写入，正式剧情仍经全部校验后提交。

`GET /api/settings` 返回 `version`、三个配置范围、`revisions`、设置页面目录 `pages` 和提示词目录 `prompt_definitions`。提供商仅返回 `has_api_key`，不返回密钥原文。

`GET /api/settings/reasoning?api_mode=...&model=...` 只读取普通 API 的本地思考规则，返回 `id/display_name`、`supported_reasoning_levels: [{effort, description}]` 和说明 `note`。不联网、不修改设置或模型列表缓存；`description` 在 Gemini 原生预算档位中包含对应思考 tokens。设置页和输入框共用此接口，ChatGPT 官方仍使用账号模型目录。

`PATCH /api/settings/{scope}` 接受：

| 字段 | 必填 | 含义 |
|---|---|---|
| `expected_revision` | 是 | 从最新读取结果取得的该范围修订号 |
| `changes` | 是 | 相对于该范围的字段路径到新值的映射 |
| `edit_session` | 否 | 将修改关联到一个可撤回的设置编辑会话 |

例如，读取到 `revisions.full_agent` 为 3 后，调整 Full Agent 输出预算：

```http
PATCH /api/settings/full_agent
Content-Type: application/json

{
  "expected_revision": 3,
  "changes": {"model.reply_tokens": 4096}
}
```

成功响应为完整的公开设置文档。下一次修改使用响应中的修订号。数组整体更新；更新 `shared.providers` 时应保留其余提供商。已有提供商的 `api_key` 留空或省略表示保留密钥，不能通过空字符串清空它。

字段路径使用点分形式，例如 `model.reply_tokens`。提供商引用、预算关联和字段类型整体验证后写入。`capability` 与 `capability_key` 由连接检查维护；设置更新统一使用分范围 PATCH 接口。

常用偏好保存在 `shared.preference_presets`，默认为空数组。每项包含唯一的 `id`、`name`（最多 80 个字符）和 `content`；三者不能仅包含空白，偏好原文保留换行和空格。通过 `PATCH /api/settings/shared` 整体更新列表，设置页标识为 `shared.preference_presets`，支持修订检查和页级撤回。常用偏好只作为可复用内容，不自动关联或更新会话的 `turn_preference`。

## 世界书库与挂载

世界书库保存独立世界书，`book` 原样保存 ST World Info 或 V2 `character_book` 的 JSON 对象（`entries` 为数组或对象），校验规则与嵌入角色卡时相同。行为说明见[世界书库指南](../guides/worldbooks.md)。

| 接口 | 请求与响应 |
|---|---|
| `GET /api/worldbooks` | 摘要数组：`id`、`name`、`revision`、`created`、`entries`（条目数） |
| `POST /api/worldbooks` | `{name, book}`，新建或导入，返回完整记录 |
| `GET /api/worldbooks/{id}` | 完整记录，含 `book` |
| `PUT /api/worldbooks/{id}` | `{name, book, expected_revision}`；修订号不一致返回 409 |
| `DELETE /api/worldbooks/{id}` | 删除书库记录；已挂载的对话保留各自副本 |
| `GET /api/worldbooks/{id}/export` | JSON 附件，条目旗标按角色卡导出的规范名写入 `extensions` |
| `GET /api/cards/{cid}/defaults` | `{persona_id, worldbook_ids, version}`，未设置时 `version` 为 0 |
| `PUT /api/cards/{cid}/defaults` | `{persona_id, worldbook_ids, expected_version}`；只影响新建对话的预选项 |
| `PUT /api/sessions/{sid}/worldbooks` | `{worldbook_ids, reload, expected_revision, operation_id}`；按顺序设定挂载，`reload` 只能包含仍挂载的书 |

`POST /api/sessions` 可选 `worldbook_ids`（按挂载顺序，不可重复）。挂载把书库条目复制进对话级角色卡的 `character_book`，每个副本获得新的条目 ID；`card_data` 保持书架副本。剧情快照的可选字段 `worldbooks` 记录 `[{id, name, revision, entries}]`：书库 ID、挂载时的名称与修订号，以及该书在本对话中的条目 ID；缺少该字段表示没有挂载。这份记录不进入卡片文档，因此不会出现在模型输入中。

调整挂载与手工编辑对话世界书相同，作为一轮设定修改提交：对话须空闲，审核策略下进入审核，生成最新回复的新版本，切换版本恢复对应的挂载。卸载和重新载入会移除该书在对话中的全部副本，包括对话中对它们的修改。书架卡同步不比较挂载条目，全量覆盖后保留它们。角色卡默认项保存在 `card_defaults` 与 `card_default_worldbooks` 中，不写入角色卡文件；删除角色卡、用户角色或世界书时由外键一并解除，复制角色卡时一并复制。

## 个人数据包

导入导出位于全局设置页面 `shared.storage`（“数据与存储”），不参与普通字段写入；该页的撤回只作用于存储限额字段。

| 接口 | 请求与响应 |
|---|---|
| `GET /api/data-transfer` | 返回可导出的用户角色、世界书、角色卡（含 `defaults`）和对话简表 |
| `POST /api/data-transfer/export` | JSON：`settings`、`include_api_keys`、`personas`、`worldbooks`、`cards`、`sessions`、`browser_preferences`；四组条目为 ID 数组，响应为 ZIP 附件 |
| `POST /api/data-transfer/preview` | multipart `file`；返回类别和条目预览、`sha256`、`settings_revisions` |
| `POST /api/data-transfer/import` | multipart `file` 与 JSON 字符串 `options`；包含预览的 `sha256`、`restore_settings`，恢复设置时还需 `settings_revisions` |

导入响应包含新 ID 数组 `personas`、`worldbooks`、`cards`、`sessions`，以及 `settings_restored`、`browser_preferences`。客户端收到结果后重新读取资源列表；恢复设置时重新建立设置编辑会话，并应用返回的浏览器偏好。

角色卡记录附带 `defaults`（`persona_id`、`worldbook_ids`），导出时只保留同一包中的用户角色和世界书；导入时随新身份重映射。对话快照中的挂载记录同样重映射到包内世界书，未随包的世界书保留原名称，按“书库中已删除”显示。

ZIP 只包含 `dataset.json` 和 `blobs/<sha256>`。清单格式为 `terrarium-tavern-personal-data`、版本 5，仍可读取版本 1–4（版本 1–3 视为没有世界书库）；版本 5 包含普通正文响应及原生结束轨迹，旧版程序拒绝读取。上传最多 256 MiB，解压最多 512 MiB，清单最多 64 MiB、文件最多 20000 个。导入校验资源哈希、记录类型和完整分支图，再在单一数据库事务中创建新身份、重映射消息和状态引用。附件写入失败时回滚数据及本次新建文件。运行中的所选对话不能导出；恢复设置时还校验全局修订号并拒绝覆盖运行中任务。

导出 API Key 默认关闭，提供商地址和标识完全一致时才保留本地已有密钥。恢复的模型能力检查结果清空。对话包保留会话自己的角色资源和上下文轨迹，排除可重建索引、模型权重、未提交回合及制卡 agent 对话/草稿；完整目录迁移使用 CLI 完整备份。

## 消息适配

模型配置的 `message_adapter` 字典整体写入，例如 `model.message_adapter`、`foreground.message_adapter`。结构为 `{"version":1,"mappings":{"chat_completions":{"state":{"role":"user","wrap":false}}}}`；省略类型使用程序默认值。只允许覆盖类型目录中 `configurable=true` 的项。

`GET /api/message-adapter/types?lane=foreground` 返回该调用链的类型目录、标签和四种协议的默认规则；`lane` 也可为 `background`、`full_agent`、`card_author`、`advisor`。

对话配置使用 `GET/PUT /api/sessions/{sid}/message-adapter`；制卡使用 `GET/PUT /api/card-author/conversations/{wid}/message-adapter`。GET 返回 `revision`、按调用链组织的 `config` 和 `lanes` 类型目录。PUT 提交 `{"expected_adapter_revision":0,"config":{...}}`，成功返回完整的新值。这个修订号独立于剧情／草稿修订号，运行中也可保存。下一次模型调用读取新值，同次调用的重试保持原快照。

全局 adapter 默认值只在创建对话时复制，分支复制父对话当前设置；全局修改不影响已有对话。持久化保存在 `message_adapter_configs`，随对话删除和备份管理。

## 页级撤回

1. `POST /api/settings/edit-sessions` 返回 `id` 和当前 `settings`，服务器同时保存基线快照。
2. 后续 PATCH 携带这个 `edit_session`，服务器记录该编辑会话最近写入的值。
3. `POST /api/settings/edit-sessions/{id}/undo` 接受页面标识和该页面所属范围的修订号，例如：

   ```json
   {"page": "full_agent.model", "expected_revision": 4}
   ```

4. `DELETE /api/settings/edit-sessions/{id}` 释放快照。

页面标识取自 `pages[].id`。撤回将该页字段恢复为打开编辑会话时的值，包括服务器保存的密钥；密钥不进入响应。服务器还会比较当前值与本编辑会话最近写入的值，防止撤回覆盖其他窗口的修改。

快照保存在服务端内存中，闲置两小时过期，服务重启后失效。撤回只处理设置值，不回滚已经执行的生成、下载、预热或清理任务。

## 错误处理

| 状态 | 典型原因 | 客户端处理 |
|---|---|---|
| 400 | 未知字段、非法关联、试图写入能力检查字段 | 保留草稿并显示校验信息 |
| 404 | 请求的记录不存在（服务端显式抛出 `NotFound`） | 显示 `detail` 原文并刷新列表 |
| 409 | 修订冲突、编辑会话过期、撤回会覆盖外部修改 | 重新读取设置或重新打开编辑会话 |
| 422 | 请求体或路径参数不符合接口类型 | 修正请求结构 |
| 500 | 程序缺陷，包括未声明的字典缺键 | 报告问题；不会伪装成 404 |

错误信息不回显提交的密钥。冲突后的重试必须先取得新版本，不能盲目替换 `expected_revision` 后覆盖其他修改。

## 数据库与设置迁移

整轮重试从 schema 9 开始：用户节点快照必须早于本轮脚本和 Tracker；Full Agent 状态为 4，Half Agent 记忆格式为 3。schema 1–8 中已有的剧情不做迁移，数据库打开会在数据库写入前拒绝，恢复旧备份也不会迁移其剧情。原数据库保留，可用旧版导出非对话资料后在新目录导入；个人数据包 1、2 中的对话同样拒绝。以下旧格式升级仅适用于没有剧情对话的数据库及独立设置资料。

当前数据库为 **schema 13**，设置文档为 **version 12**，剧情快照中的 `memory_document.version` 为 **3**。三者分别标识数据库结构、设置文档和记忆格式；Full Agent 快照中的 `full_agent.version` 为 **4**（handoff 由模型维护，不含章节手工标记）。版本 1 的自由文本 handoff 和未发行的版本 2 开发数据均不迁移，读取时明确报错并提示新建对话，不改写旧快照。

记忆格式 version 2 除活跃记录外，在消息快照的 `memory_retired` 中保存该消息新增或修改的归档条目（见[归档与召回](../design/memory-consistency.md#归档与召回)）。新增字段同样属于持久格式变化，因此递增版本；version 1 的记忆不迁移，读取时报错并提示新建对话，不改写旧快照。

存储层接受 schema 1–13，先只读核对版本与设置。数据库版本低于 13、设置文档需要迁移或会话表缺少新增列时，在任何写入前生成一份 `before-upgrade-schema<旧版本>-settings<旧版本>-*.sqlite3` 备份；已是当前格式则不备份。随后的步骤均可重复执行：初始化参谋数据表（schema 4），添加 `sessions.update_variables` 等新增列（schema 5），原地把 `card_editor_conversations/progress/messages/traces` 改名为 `card_author_*`，并把消息适配设置与已发送缓存断点中的 `card_editor` 通道改为 `card_author`，对话身份与内容不变（schema 6）。设置文档 version 2 的 `shared.card_editor`、`shared.card_editor_config` 原样移到 `shared.card_author`、`shared.card_author_config` 并升为 version 3；设置导入同样接受旧键名。旧编辑对话保留原身份。随后创建世界书库与角色卡默认项表（schema 10）。全部步骤完成后写入 schema 13；中途中断时下次启动从头重跑。其他数据库版本在写入前报错。

schema 10 新增 `worldbooks`、`card_defaults`、`card_default_worldbooks` 三张表和剧情快照的可选字段 `worldbooks`（见[世界书库与挂载](#世界书库与挂载)）。schema 9 的剧情对话照常升级，升级前同样先备份；已有快照缺少该字段即没有挂载，无需改写。

schema 11 在制卡对话的草稿项（`card_author_conversations.payload.cards[]`）新增可选字段 `worldbook`：存在时该项是世界书库的一本书的草稿，文档为 `{name, character_book}`，`card` 为空，`base_revision` 是库书修订号；缺少该字段的项照旧是角色卡草稿，无需改写。引用与保存接口见[制卡 agent 工具与接口](card-author-tools.md#http-接口)。

schema 8 / 设置 version 5 添加消息的独立辅助记录、会话的 `kemini` 标记（已有对话为普通对话）以及默认关闭的实验设置。原有提供商连接与故事正文保持原样；此前 schema 7 / 设置 version 4 引入的按提供商连接记录继续保留。设置导入接受旧版并使用相同迁移规则；旧程序不能直接打开新版数据库。

迁移按升级前的实际生效值创建独立设置：继承 Writer 的 Full Agent 模型、继承 Tracker 的制卡 agent 模型会被复制为明确配置；共用运行参数复制到两种模式。已有提供商、密钥和自定义文本保留。旧程序回退应使用迁移前备份，不能直接打开已升级数据库。迁移覆盖已实现的设置和数据库格式转换，历史行为按对应版本记录核对。

其他配置迁移由同一设置层管理：

- 旧记忆与设定维护提示词中的自定义原则按用途合并到 `tracker_system_prompt`；默认提示词由程序更新。
- 旧提供商中的协议和路径迁移到引用它的模型；模型独立保存接入方式。
- 已保存应用提示词中的“玩家”和英文 `player` 规范为“用户”；角色卡、用户角色名称和剧情原文不参与替换。实际发生变化的范围才增加修订号。
- 缺少缓存模式的配置迁移后，正文使用本地控制，Tracker 和制卡 agent 使用云端策略；已有会话的手动断点及跟进参数保留。

设置 version 5 → 6 为 `shared.kemini` 增加 `clear_prompt`、`info_prompt`、`transport_prompt`、`tool_description` 和 `content_description`。新增字段写入其可见默认值，已有文本与开关原样保留。Kemini 文本统一从共享设置读取并支持本页撤回及导入导出；不再返回另一份只读 `kemini_protocol`。

设置 version 6 → 7 增加 `roleplay_prompt`、`content_prompt`、`output_prompt`，对应角色扮演追加要求、正文约束和末尾输出提示。仅当 CLEAR、资料起始、自检文本完全匹配旧默认值时，替换其中的独立边界标签、Interleaving 包装及相关引用。小说填充、全部自定义文本和开关原样保留；十三项文本均以数据库中的保存值为准。

设置 version 7 → 8 为 `half_agent` 增加 `memory_recall_count`（默认 3）与 `memory_recall_filter`（默认关闭），为 `full_agent.tools` 增加 `chapter_tokens`（默认 512）与 `chapters_total_tokens`（默认 4096）。新增字段写入其可见默认值，已保存的其他设置原样保留，两个范围的修订号各加一。

设置 version 8 → 9 允许显式的 Android `cpu` / `q8_0` GGUF 配置。已有 CUDA 模型、量化、设备、版本与所有其他设置原样保留；用户在本地模型页选择手机配置后才写入新值。升级前沿用完整数据库备份，旧程序不能直接打开 version 9 设置。

迁移重复执行不覆盖已迁移的配置。升级操作与恢复步骤见[安装与维护](../guides/maintenance.md#备份升级与回退)。

设置 version 10 → 11 为两种模式的 `retrieval` 增加 `index_timeout`（单条向量索引超时，整数秒，范围 1–3600，默认 60）。升级前备份数据库，两个模式的修订号各加一；其他已保存设置与剧情原文保留。界面值、运行时使用值和导入导出值一致，重复迁移保留用户已经调整的时限。

设置 version 11 → 12 扩展 `shared.local_models.{embedding,reranker}.device` 的 Android Q8_0 设备选择为 `cpu`、`opencl:0`、`hexagon:0`。升级前备份数据库，已有设备值与其他配置原样保留，不新增覆盖字段；导入导出使用相同格式。

## 实现与验证

| 入口 | 职责 |
|---|---|
| [core/settings.py](../../src/tavern/core/settings.py) | 设置模型、迁移、运行时投影 |
| [application/settings_api.py](../../src/tavern/application/settings_api.py) | 页面目录、修订校验、公开视图和撤回 |
| [storage/store.py](../../src/tavern/storage/store.py) | 数据库版本检查、备份与持久化 |
| [useSettingsEditor.ts](../../web/composables/useSettingsEditor.ts) | 保存队列、草稿与响应合并 |
| [test_settings_document.py](../../tests/backend/test_settings_document.py) | 模式隔离、迁移、密钥保护与并发行为 |

schema 13 为 `full_agent_progress.payload` 引入 version 3：保存稳定的 `output_id`、`story`（content 与 auxiliary）、原生 `model_completed` 状态及显式续接原因。version 2 的 `roleplay`／`finish_requested` 和未标版本的旧进度（version 1）读取时适配，保留正文身份、成功工具及脚本副作用；尚未执行的旧交付／收尾仅在迁移续接时处理。旧原始轨迹保留审计，模型回放改为普通正文和业务工具；未知版本拒绝读取。Full Agent 剧情快照仍为 version 4，消息适配的既有键保留。升级前完整备份，旧程序不能打开 schema 13；个人数据包同步为 version 5，防止旧程序误读新轨迹。

设置 version 9 → 10 增加 `full_agent.tools.max_duration_seconds`，整数秒、含边界 30–7200，默认 900。Full Agent 范围修订号增加，其他设置与用户保存的提示词原样保留。字段在“工具与记忆”页可编辑，参与范围保存、撤回和个人设置导入导出；运行中的回合继续使用开始时的快照。总时限覆盖模型、工具、自动整理和正文回调，等待用户审核不计入；显式继续取得新时限。
