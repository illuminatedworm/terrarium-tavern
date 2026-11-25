# 设置持久化与 API

设置按范围保存，用修订号处理并发修改。当前格式与旧格式的读取规则见[数据库与设置迁移](#数据库与设置迁移)。界面操作见[设置指南](../guides/settings.md)，模型传输字段见[模型接入参考](model-api.md)。

## 配置范围与修订号

数据库中的设置文档由两个独立范围组成：

| 范围 | 主要内容 | 模型字段 |
|---|---|---|
| `story` | 模型、工具与记忆、检索、提示词、全局媒体参数及默认修改保存方式 | `model`、`official_model` |
| `shared` | 提供商、参谋 agent、制卡 agent、联网服务、本地模型、偏好及清理参数 | `card_author`、`advisor.model`、`advisor.official_model` |

**提供商连接**

- 每个模型配置的 `provider_id` 指向当前提供商，平铺的 `api_mode`、`api_path`、`model` 是当前生效值。
- `provider_connections` 按提供商 ID 保存其他提供商的这三个字段，不能包含当前提供商，否则报重复连接配置，以免历史值覆盖可见的编辑。
- 切换提供商时，原提供商的当前值移入记录，再取出目标提供商的记录作为当前值；没有记录时使用空模型、空路径和 `chat_completions`（官方渠道固定为 Responses）。
- 该映射整体 PATCH（如 `model.provider_connections`），与当前提供商及三个字段在一次请求中保存，支持页面撤回和设置导入导出。

游玩、制卡和参谋 agent 的对话级 `model_selection` 包含当前 `provider_id/model`，以及可选的 `provider_models`（其他提供商在此对话中上次选择的模型名）。后者由服务器在切换时更新，不改变全局模型名。对话所选的协议和路径通过相应角色的 `provider_connections` 查找，不从另一提供商继承。游玩和制卡允许 `model: ""`，表示所选提供商尚未配置模型，此时禁止发送。现有对话与分支继承这些记录。

`policy` 只控制保存方式：`automatic` 自动保存，`review` 在实际变量、角色设定或世界书有变化时审核整轮；记忆和运行记录不触发审核。旧值 `directed`／`evolving` 读作 `automatic`，`preview` 读作 `review`；会话级 `policy: null` 沿用全局。设定写入只由请求的 `allow_content_edit` 授权，变量任务由 `update_variables` 开启，授权与保存方式分别校验。

每个范围有自己的 `revisions` 值。更新共享提供商可能使其他范围的模型能力检查失效，所以一次响应可能带回多个范围的新修订号。客户端应合并响应中的实际值，而不是只递增所提交范围的本地计数。

`SettingsDocument.runtime(channel)` 为一次操作生成深拷贝配置：执行中的回合继续使用开始时的快照，保存后的配置用于后续操作。剧情模型与制卡 agent 都有明确的模型配置。

## 读取与写入

`GET /api/settings` 返回 `version`、两个配置范围、`revisions`、设置页面目录 `pages` 和提示词目录 `prompt_definitions`。提供商只返回 `has_api_key`，不返回密钥原文。

`GET /api/settings/reasoning?api_mode=...&model=...` 只读取普通 API 的本地思考规则，返回 `id/display_name`、`supported_reasoning_levels: [{effort, description}]` 和说明 `note`。它不联网，不修改设置或模型列表缓存；在 Gemini 原生预算档位中，`description` 包含对应的思考 tokens。设置页和输入框共用此接口，SIWC 使用账号模型目录。

`PATCH /api/settings/{scope}` 接受：

| 字段 | 必填 | 含义 |
|---|---|---|
| `expected_revision` | 是 | 从最新读取结果取得的该范围修订号 |
| `changes` | 是 | 相对于该范围的字段路径到新值的映射 |
| `edit_session` | 否 | 将修改关联到一个可撤回的设置编辑会话 |

例如，读取到 `revisions.story` 为 3 后，调整剧情模型输出预算：

```http
PATCH /api/settings/story
Content-Type: application/json

{
  "expected_revision": 3,
  "changes": {"model.reply_tokens": 4096}
}
```

- 成功响应为完整的公开设置文档，下一次修改使用其中的修订号。
- 字段路径使用点分形式，如 `model.reply_tokens`。提供商引用、预算关联和字段类型整体校验后写入。
- 更新 `shared.providers` 时应带上其余提供商。已有提供商的 `api_key` 留空或省略表示保留密钥，不能用空字符串清空。
- `capability` 与 `capability_key` 由连接检查维护，不能通过 PATCH 写入。

常用偏好保存在 `shared.preference_presets`，默认为空数组。每项包含唯一的 `id`、`name`（最多 80 个字符）和 `content`，三者都不能只含空白，偏好原文保留换行和空格。通过 `PATCH /api/settings/shared` 整体更新列表；设置页标识为 `shared.preference_presets`，支持修订检查和页级撤回。常用偏好只是可复用内容，不会自动关联或更新会话的 `turn_preference`。

## 单项设置字段

**主提示词选项**。剧情模型的 `system_prompt`、 `shared.advisor.system_prompt` 和 `shared.card_author_config.system_prompt` 使用同一种结构。每项包含 `active`（整数 1～4，初始 1）及四个按编号排列的 `options`。每个选项保存 `text` 和 `name` 字符串，空名称显示编号。可通过相对路径 `system_prompt.options.1.text` 或 `system_prompt.options.1.name` 单独更新第二项，索引从 0 开始。通过 `system_prompt.active` 切换生效项，运行时读取对应的完整文本，包含显式空白。提示词目录中这三项的 `choices` 为 `true`，`default` 给出第一项的内置文本。

**`story.tools.parallel_tool_calls`**：严格布尔值，默认 `true`，旧设置缺少时按开启处理。对应“工具与记忆”页的“单次允许多个工具调用”，只控制剧情模型。关闭示例：`PATCH /api/settings/story`，`changes` 为 `{"tools.parallel_tool_calls":false}`。

**`shared.model_request_mode`**：`auto`（默认，保持各调用方原有方式）、`stream`（普通 API 统一流式）或 `non_stream`（普通 API 统一非流式）。通过 `PATCH /api/settings/shared` 写入，如 `{"model_request_mode":"stream"}`；属于 `shared.web` 页面（“联网与代理”），支持该页撤回。配置进入每次操作的运行时快照，覆盖各角色的模型生成请求。OpenAI 官方渠道始终流式；独立的 compact、向量、目录和缓存管理端点使用各自的请求方式。

**`shared.debug_failed_requests`**：严格布尔值，默认 `false`，属于 `shared.misc` 页，支持自动保存、页面撤回和个人设置导入导出，从下一次操作生效。开启后保留失败的模型生成请求的原始 HTTP 证据，成功请求不落盘。

- `GET /api/debug/failed-requests?offset=0` 按时间倒序分页返回 HTTP 信息（每页最多 100 条）和记录总数。`offset` 是从 0 开始的记录偏移条数，默认 0，无固定上界，越过末尾返回空列表。
- `GET /api/debug/failed-requests/{UUID}` 下载对应 ZIP，不提供任意路径访问。
- 记录格式、正文限额、脱敏和清理规则见[杂项设置](../guides/settings.md#杂项)。

**`shared.debug_story_prefix`**：严格布尔值，默认 `false`，属于 `shared.misc` 页，支持自动保存、页面撤回和个人设置导入导出，从下一次剧情运行生效。只在运行时内存中保留最近两次 SDK 序列化后的生成请求正文，比较结果通过 `story_prefix_debug` 回合事件返回，不包含参数原文：

- `payload.message` 用于界面通知。消息参数（`messages`／`input`／`contents`）一致或数组正常追加时显示“公共前缀与首个差异未错位”，否则显示“公共前缀或首个差异发生错位”；其他参数只显示“一致”或“不一致”。
- `payload.parameters` 按实际的顶层 JSON 参数分别返回 `parameter`、`previous_bytes`、`current_bytes`、`status`；两侧都存在的参数另返回 `common_prefix_bytes` 和 `first_difference_byte`（从 0 开始，完全相同时为 `null`）。`status` 为 `identical`、`array_extended`、`changed`、`added` 或 `removed`；不存在的一侧字节数为 `null`。
- 首次请求只通知已建立基线，参数结果为空。比较边界与暂存清理见[杂项设置](../guides/settings.md#杂项)。

**`shared.advisor`**：参谋 agent 配置。`model` 与 `official_model` 分别引用同渠道的供应商；`system_prompt` 进入共享提示词目录，支持显式保存。故事级覆盖、默认范围、工具与接口见[参谋 agent 设计](../design/advisor.md)。

**`shared.unicode_story_enabled`** 是「杂项」页的局部 Unicode 码点传输总开关，接受严格布尔值，默认 `false`。`PUT /api/sessions/{sid}/story/tools` 接收可选布尔 `unicode_story`，省略时保留当前对话值，与工具设置共用 `expected_revision` 及会话互斥检查。新回合在两个值均为 `true` 时采用局部码点格式，失败续接沿用原回合的格式。 [设置指南](../guides/settings.md#unicode-码点传输)

**`shared.kemini`**：属于 `shared.misc` 页面，包含默认关闭的 `enabled`、`normal_disclaimer_enabled` 及十三项可编辑文本，复用共享设置的 PATCH、修订检查和页面撤回。

- `enabled` 只允许创建并继续 Kemini 实验对话，本身不改变任何对话。为 `false` 时，`POST /api/sessions` 拒绝 `kemini: true`，已有 Kemini 实验对话的生成回合也被拒绝。
- 免责声明只在 Kemini 实验对话中、且 `normal_disclaimer_enabled` 为 `true` 时生效。主开关关闭时保留其保存值，界面置灰。
- `thinking_prompt` 和 `disclaimer_text` 保存完整提示及其格式要求，Runtime 不另加包装。默认值来自原预设的对应条目，去掉了 SillyTavern 宏；独立边界标签仍用 Markdown 分隔，Interleaving 三段式示例和自检原文完整保留在同一条 `kemini_control` 消息中；CLEAR 和末尾输出提示只引用名称，不放孤立的开标签。
- `roleplay_prompt` 位于助手确认之前，`content_prompt` 位于自检之后，`output_prompt` 位于传输指令之后，均可显式保存。请求只读取已保存的文本。

## 会话字段与事件

`PUT /api/sessions/{sid}/options` 接受 `expected_revision` 及需修改的会话字段，省略的字段保持原值。`update_variables` 是对话级布尔偏好，默认 `false`，在输入框勾选时立即保存，分支和个人数据包保留；回合请求仍显式传入当轮的 `update_variables`。

`POST /api/sessions` 和 `POST /api/sessions/{sid}/turns` 收到未定义的字段时返回 422。

会话的 `kemini` 是创建时固定的布尔值（`POST /api/sessions` 的可选字段，默认 `false`），只允许 `channel_origin` 为 `standard`。会话更新接口不能修改它；分支与数据包原样继承，导入时按同样约束校验。剧情消息的 `auxiliary` 为 `{kind: thinking|disclaimer|extra, text: string}[]`，旧消息为空列表；`kemini_auxiliary` SSE 事件的 `items` 替换当前响应的辅助记录，沿用事件序号去重。

剧情模型流式接收时，只把正文工具的 content 参数通过 `text` 事件追加到未保存的预览，普通过程说明不进入预览。`text_reset` 以 `payload.text` 替换预览，用于清空被拒绝的正文提案、按完整结果校正，或显示脚本处理后的正文。已接收的正文在后续工具请求期间保留。客户端必须按事件序号去重，完整重放时从空文本开始；预览快照与对应事件原子写入，正式剧情仍经全部校验后提交。

`PUT /api/sessions/{sid}/opening` 接受 `text` 与 `expected_revision`，只允许用于未锁定、且当前只有开场的对话。隔离初始化成功后复核修订号与锁定状态，原子保存原文、model、display 和快照，并增加修订号。`GET /api/sessions/{sid}` 的 `opening_editable` 表示当前是否可编辑。锁定规则见 [schema 14](#数据库-schema-变更)。

## SIWC 账号接口

提供商保存 `auth_mode: api_key | chatgpt_subscription`（缺省为 `api_key`）及可空的 `chatgpt_account_id`。账号引用可以随设置迁移，但账号元数据、Client ID、host ID 和令牌不进入设置数据库及导出包。没有本地注册记录的引用显示为需要重新连接。

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
| `POST /api/auth/chatgpt/welcome` | 保存首次 SIWC 用量说明的确认状态 |

API 不返回授权 URL、state、nonce、PKCE verifier 或令牌。OAuth 回调由独立的 `127.0.0.1` 临时监听器处理，路径固定为 `/auth/callback`。`generation` 是授权会话的非秘密版本号，仅用于账号切换后刷新客户端缓存。

模型列表接口保持 `models: string[]`，SIWC 额外返回 `model_details`：每项包含 `id/display_name`，目录提供能力时还包含 `supported_reasoning_levels: [{effort, description}]`、`default_reasoning_level` 和 `unavailable_reasoning_levels: [{effort, reason}]`。支持的档位是账号目录与当前 SIWC 接口契约的交集，保持目录顺序。

可操作的 SIWC 错误响应包含 `detail/code/upstream_status/request_id/usage_url`。程序不会从额度错误推算套餐重置时间，也不会回退到其他计费凭据。

## 世界书库与挂载

世界书库保存独立世界书。`book` 原样保存 ST World Info 或 V2 `character_book` 的 JSON 对象（`entries` 为数组或对象），校验规则与嵌入角色卡时相同。行为说明见[世界书库指南](../guides/worldbooks.md)。

| 接口 | 请求与响应 |
|---|---|
| `GET /api/worldbooks` | 摘要数组：`id`、`name`、`revision`、`created`、`entries`（条目数） |
| `POST /api/worldbooks` | `{name, book}`，新建或导入，返回完整记录 |
| `POST /api/worldbooks/prepare-import` | multipart `file`，返回经校验和用途转换的世界书 JSON，供编辑器加入草稿 |
| `GET /api/worldbooks/{id}` | 完整记录，含 `book` |
| `PUT /api/worldbooks/{id}` | `{name, book, expected_revision}`；修订号不一致返回 409 |
| `DELETE /api/worldbooks/{id}` | 删除书库记录；已挂载的对话保留各自副本 |
| `GET /api/worldbooks/{id}/export` | JSON 附件，条目旗标按角色卡导出的规范名写入 `extensions` |
| `GET /api/cards/{cid}/defaults` | `{persona_id, worldbook_ids, version}`，未设置时 `version` 为 0 |
| `PUT /api/cards/{cid}/defaults` | `{persona_id, worldbook_ids, expected_version}`；只影响新建对话的预选项 |
| `PUT /api/sessions/{sid}/worldbooks` | `{worldbook_ids, reload, expected_revision, operation_id}`；按顺序设定挂载，`reload` 只能包含仍挂载的书 |

`POST /api/sessions` 可选 `worldbook_ids`（按挂载顺序，不可重复）。挂载把书库条目复制进对话级角色卡的 `character_book`，每个副本获得新的条目 ID；`card_data` 保持书架副本。剧情快照的可选字段 `worldbooks` 记录 `[{id, name, revision, entries}]`：书库 ID、挂载时的名称与修订号，以及该书在本对话中的条目 ID；缺少该字段表示没有挂载。这份记录不在卡片文档里，因此不会进入模型输入。

- 调整挂载与手工编辑对话世界书一样，作为一轮设定修改提交：对话须空闲，审核策略下进入审核，生成最新回复的新版本；切换版本时恢复对应的挂载。
- 卸载和重新载入会移除该书在对话中的全部副本，包括对话中对它们做的修改。
- 书架卡同步不比较挂载条目，全量覆盖后保留它们。
- 角色卡默认项保存在 `card_defaults` 与 `card_default_worldbooks` 中，不写入角色卡文件。删除角色卡、用户角色或世界书时由外键一并解除，复制角色卡时一并复制。

## 个人数据包

导入导出位于全局设置页面 `shared.storage`（“数据与存储”），不走普通字段写入；该页的撤回只作用于存储限额字段。

| 接口 | 请求与响应 |
|---|---|
| `GET /api/data-transfer` | 返回可导出的用户角色、世界书、角色卡（含 `defaults`）和对话简表 |
| `POST /api/data-transfer/export` | JSON：`settings`、`include_api_keys`、`include_novel_files`（默认 `true`）、`personas`、`worldbooks`、`cards`、`sessions`、`browser_preferences`；四组条目为 ID 数组，响应为 ZIP 附件 |
| `POST /api/data-transfer/preview` | multipart `file`；返回类别和条目预览、`sha256`、`settings_revisions` |
| `POST /api/data-transfer/import` | multipart `file` 与 JSON 字符串 `options`；包含预览的 `sha256`、`restore_settings`，恢复设置时还需 `settings_revisions` |

导入响应包含新 ID 数组 `personas`、`worldbooks`、`cards`、`sessions`，以及 `settings_restored`、`browser_preferences`。客户端收到后重新读取资源列表；恢复了设置时重新建立设置编辑会话，并应用返回的浏览器偏好。

对话、角色卡和世界书按各自的原列表顺序导出。导入后的新条目保留数据包中的相对顺序，导入响应中的对应 ID 数组也按此顺序返回。

角色卡记录附带 `defaults`（`persona_id`、`worldbook_ids`），导出时只保留同一包中的用户角色和世界书，导入时随新身份重映射。对话快照中的挂载记录同样重映射到包内世界书；未随包的世界书保留原名称，显示为“书库中已删除”。

ZIP 只包含 `dataset.json` 和 `blobs/<sha256>`。清单格式为 `terrarium-tavern-personal-data`。导入支持从版本 1 到[当前导出版本](#当前格式与旧剧情)的所有格式。旧版程序拒绝读取新版包。

| 版本 | 新增 |
|---|---|
| 1–3 | 视为没有世界书库；旧剧情的兼容限制见[数据库与设置迁移](#数据库与设置迁移) |
| 5 | 普通正文响应及原生结束轨迹 |
| 6 | 开场白的永久锁定状态 |
| 7 | 参考文件与调查记录 |
| 8 | 调查者自动销毁选项 |
| 9 | 正文分段边界 |
| 10 | 校验交互图形 |
| 11 | 用户消息的独立场外发言，导入时核对非空场外发言与模型消息中的原文一致 |
| 12–14 | 主提示词选项、正文码点交付选项及新版剧情记录 |
| 15 | 使用 `story` 保存剧情快照和消息适配，工具轨迹使用 `story_trace` |
| 16 | 角色卡、世界书和剧情快照使用 `extensions.terrarium.purpose` 表示条目用途 |
| 17 | 对话媒体资源、任务和消息引用 |
| 18 | 资料调查者的联网配置和无文件调查记录 |
| 19 | 动态世界书召回时冻结的预览与余文 |
| 20 | 批量展开世界书时每项对应的原注入锚点，导入时随分支消息重映射 |
| 21 | 按配音、生图协议保存媒体设置和任务的原生请求参数 |
| 22 | 会话保存媒体配置来源，全局设置包含 `story.media` |
| 23 | 配音和生图各自保存来源，角色声线按剧情模型与配音模型组合保存 |

- 限额：上传最多 256 MiB，解压最多 512 MiB，清单最多 64 MiB、文件最多 20000 个。
- 导入校验资源哈希、记录类型和完整分支图，再在单一数据库事务中创建新身份、重映射消息和状态引用。附件写入失败时回滚数据及本次新建的文件。
- 运行中的所选对话不能导出；恢复设置时还校验全局修订号，并拒绝覆盖运行中的任务。
- 导出 API Key 默认关闭；导入时只有提供商地址和标识完全一致才保留本地已有密钥。恢复后的模型能力检查结果清空。
- 对话包保留会话自己的角色资源和上下文轨迹，排除可重建的索引、模型权重、未提交回合以及制卡 agent 对话/草稿。完整目录迁移使用 CLI 完整备份。

## 消息适配

模型配置的 `message_adapter` 字典整体写入，如 `model.message_adapter`。结构为 `{"version":1,"mappings":{"chat_completions":{"handoff":{"role":"user","wrap":false}}}}`，省略的类型使用程序默认值；只能覆盖类型目录中 `configurable=true` 的项。

`GET /api/message-adapter/types?lane=story` 返回该调用链的类型目录、标签和四种协议的默认规则；`lane` 也可为 `card_author` 或 `advisor`。

`story` 的 `novel_research`（资料调查消息）和 `novel_status`（资料调查运行状态）允许配置发送角色：Google GenAI（`gemini`）默认 `user`，其他协议默认 `system`，显式设置的映射优先。全局「资料调查」页的“消息适配”只展示这两个类型，直接编辑 `story.model.message_adapter`，与“模型与生成 → 普通 API”共用配置。该页的撤回范围同时包含 `novel_research` 和 `model.message_adapter`；撤回共享映射时两处入口同步更新。

对话配置使用 `GET/PUT /api/sessions/{sid}/message-adapter`，制卡使用 `GET/PUT /api/card-author/conversations/{wid}/message-adapter`。GET 返回 `revision`、按调用链组织的 `config` 和 `lanes` 类型目录。PUT 提交 `{"expected_adapter_revision":0,"config":{...}}`，成功返回完整新值。这个修订号独立于剧情／草稿修订号，运行中也可保存；下一次模型调用读取新值，同一次调用的重试保持原快照。

全局 adapter 默认值只在创建对话时复制，分支复制父对话的当前设置；全局修改不影响已有对话。持久化保存在 `message_adapter_configs`，随对话删除和备份管理。

## 页级撤回

1. `POST /api/settings/edit-sessions` 返回 `id` 和当前 `settings`，服务器同时保存基线快照。
2. 后续 PATCH 携带这个 `edit_session`，服务器记录该编辑会话最近写入的值。
3. `POST /api/settings/edit-sessions/{id}/undo` 接受页面标识和该页面所属范围的修订号，例如：

   ```json
   {"page": "story.model", "expected_revision": 4}
   ```

4. `DELETE /api/settings/edit-sessions/{id}` 释放快照。

页面标识取自 `pages[].id`。撤回把该页字段恢复为打开编辑会话时的值，包括服务器保存的密钥（密钥不进入响应）。服务器还会比较当前值与本编辑会话最近写入的值，防止撤回覆盖其他窗口的修改。

快照保存在服务端内存中，闲置两小时过期，服务重启后失效。撤回只处理设置值，不回滚已经执行的生成、下载、预热或清理任务。

## 错误处理

| 状态 | 典型原因 | 客户端处理 |
|---|---|---|
| 400 | 未知字段、非法关联、试图写入能力检查字段 | 保留草稿并显示校验信息 |
| 404 | 请求的记录不存在（服务端显式抛出 `NotFound`） | 显示 `detail` 原文并刷新列表 |
| 409 | 修订冲突、编辑会话过期、撤回会覆盖外部修改 | 重新读取设置或重新打开编辑会话 |
| 422 | 请求体或路径参数不符合接口类型 | 修正请求结构 |
| 500 | 程序缺陷，包括未声明的字典缺键 | 报告问题（不会伪装成 404） |

错误信息不回显提交的密钥。冲突后须先取得新版本再重试，不能直接替换 `expected_revision` 覆盖他人的修改。

## 会话媒体接口

`story.media` 保存全局配音、生图参数及每轮额度，通过游玩设置的修订号、局部更新和页面撤销接口编辑。`GET /api/media/protocols` 可在没有故事时读取协议预设与声音目录。全局 `speech` 与 `image` 各含 `active_channel`、`standard`、`chatgpt` 和其他提供商的 `remembered` 配置。每轮额度单独保存在对应用途下。媒体编辑器通过 `POST /api/settings/models?media=true` 更新当前提供商的模型目录，`api_path` 留空时请求 `/models`。`POST /api/media/preview` 接受 `kind`、`channel`、当前编辑的 `profile`，以及配音的 `text` 和 `voice` 或生图的 `prompt`。成功返回媒体字节，预览使用所选渠道的额度，只在当前面板临时展示。下表路径位于 `/api/sessions/{sid}/media`。任务冻结生成参数，凭据仅从已选提供商连接读取。

`gemini_speech.prompt_prefixes` 和 `cloud_voice_prefixes` 分别标识旧版预设型号与已知支持项目音色的型号。对其他模型名，显式选择 `instruction_format: prompt` 时使用旧版预设音色。编辑或启用配音配置时会校验音色，旧设置和历史任务可按原值读取。修正角色绑定或默认音色后，可用「按当前设置重新生成」重做旧片段。

| 方法与后缀 | 用途 |
|---|---|
| `GET /` | 返回生效配置 `config`、原始 `selection`、`selection_token`、当前 `story_model`、来源 `source`（global / story / mixed）、全局参数 `defaults`、修订号、无密钥的连接信息、协议与声音目录及参考图库。完整会话接口的 `media_source` 和 `media_config` 使用同一口径。`?library=false` 仅读取设置。 |
| `PUT /` | `{expected_revision, expected_selection, config}` 保存本故事媒体设置。`expected_selection` 取读取时的 `selection_token`，可检测期间新增的角色绑定。每项服务含授权 `enabled` 及 `custom`，其值为空时跟随全局。`view_images` 和按剧情模型及配音模型区分的 `voice_tables` 属于故事。运行中剧情和上下文整理期间拒绝修改。关闭功能取消待发任务并尝试停止运行请求。 |
| `POST /jobs` | `{message_ids}` 查询最多100条回复的媒体任务及产物元信息，不读取媒体字节。 |
| `POST /jobs/{jid}/retry` | `{operation_id}` 使用原参数重试失败／中断／取消任务；操作编号1–100字符，同一操作重复提交返回同一结果。 |
| `POST /jobs/{jid}/regenerate` | `{operation_id}` 使用当前设置新建重做任务，保留原产物；同样按操作编号去重。 |
| `POST /jobs/{jid}/cancel` | 停止本地任务；不承诺撤销服务端费用。 |
| `GET /events` | SSE 发送带序号的状态更新；重连先刷新数据库权威状态。 |
| `GET /assets/{aid}` | 返回本故事的媒体文件，支持浏览器音频范围请求。 |
| `POST /references` | multipart 的 `file/expected_revision/name/description`，导入最多32 MiB的PNG、JPEG或静态WebP图片并授权为常驻参考。 |
| `POST /card-reference` | `{expected_revision,key,name,description,selected}` 从当前会话卡片选择图片，不接受任意路径或远程URL。 |
| `PUT /references/{aid}` | `{expected_revision,name,description,selected}` 修改图片说明和常驻选择；此前已查看的历史图片仍保留。 |

配音文本不做正文匹配、来源或逐字校验。任务有独立的 `queued/running/completed/failed/interrupted/cancelled` 状态；默认最多两个外部任务并行，单任务超时默认900秒，可设置5–3600秒。未知远端结果不会自动重发。图片目录中已给出的编号和真正看图的引用均随历史保留，分支不会把同一编号重新用于另一张图。完整操作说明见[角色配音与剧情插图](../guides/story.md#角色配音与剧情插图)。

## 数据库与设置迁移

### 当前格式与旧剧情

| 格式 | 当前版本 |
|---|---|
| 数据库 schema | **40** |
| 设置文档 | **27** |
| 剧情快照 `story.version` | **4**（handoff 由模型维护，不含章节手工标记） |
| 个人数据包 | **26** |
| 剧情回合续接进度 | **13** |

整轮重试从 schema 9 开始。缺少可恢复回合结构的旧数据库会在写入前给出处理说明。对可升级的数据库，程序先备份，再迁移已有的分支、回合及快照。旧数据包预览会列出格式过旧的故事，并提供同包可导入的资料。

- 快照版本 1 的自由文本 handoff 与版本 2 的开发数据需要用对应旧版读取。

### 升级流程

存储层接受 schema 1 至当前版本，先只读核对版本与设置。数据库需要升级、设置需要迁移或会话表缺少新增列时，在任何写入前生成 `before-upgrade-schema<旧版本>-settings<旧版本>-*.sqlite3` 备份。剧情迁移和格式版本写入在同一事务中完成。其他数据库版本在写入前报错。

旧程序不能直接打开已升级的数据库，回退应使用迁移前的备份。重复迁移不覆盖已迁移的配置。设置导入接受旧版本并使用相同的迁移规则。升级操作与恢复步骤见[安装与维护](../guides/maintenance.md#备份升级与回退)。

### 数据库 schema 变更

下文以当前字段名说明各版本的数据变化。

- **4**：初始化参谋 agent 数据表。
- **5**：添加 `sessions.update_variables` 等新增列。该偏好不从历史回合推断。
- **6**：把 `card_editor_conversations/progress/messages/traces` 原地改名为 `card_author_*`，并把消息适配设置与已发送缓存断点中的 `card_editor` 通道改为 `card_author`；对话身份与内容不变。
- **7**（设置 version 4）：引入按提供商保存的连接记录。
- **8**（设置 version 5）：添加消息的独立辅助记录、会话的 `kemini` 标记（已有对话为普通对话）和默认关闭的实验设置。原有提供商连接和故事正文不变。
- **9**：整轮重试格式，见上文。
- **10**：新增 `worldbooks`、`card_defaults`、`card_default_worldbooks` 三张表和剧情快照的可选字段 `worldbooks`（见[世界书库与挂载](#世界书库与挂载)）。schema 9 的剧情对话照常升级；已有快照缺少该字段即视为没有挂载，无需改写。
- **11**：制卡对话的草稿项（`card_author_conversations.payload.cards[]`）新增可选字段 `worldbook`：存在时该项是书库中一本书的草稿，文档为 `{name, character_book}`，`card` 为空，`base_revision` 是库书修订号；缺少该字段的项仍是角色卡草稿，无需改写。引用与保存接口见[制卡 agent 工具与接口](card-author-tools.md#http-接口)。
- **13**：`story_progress.payload` 升为 version 3，保存稳定的 `output_id`、`story`（content 与 auxiliary）、原生 `model_completed` 状态和显式续接原因。version 2 的 `roleplay`／`finish_requested` 和未标版本的旧进度（version 1）读取时适配，保留正文身份、成功工具和脚本副作用；尚未执行的旧交付／收尾只在迁移续接时处理。旧原始轨迹保留供审计，模型回放改为普通正文和业务工具；未知版本拒绝读取。剧情快照仍为 version 4，消息适配的既有键保留。个人数据包同步升为 version 5，以免旧程序误读新轨迹。
- **14**：增加 `sessions.opening_locked`。新对话的首次生成、咨询或上下文整理任务被接收时，在同一事务中永久锁定开场白，失败或取消不回退；手工设定和卡片本地交互不锁定。旧会话默认锁定，分支继承原状态；个人数据包 version 6 保留该字段，旧包导入时锁定。编辑接口见[会话字段与事件](#会话字段与事件)。
- **15、16**：schema 15 引入的 `context_token_samples` 在 16 改为保存最新请求观测：按所属会话／工作区与用途隔离，保存来源、内容指纹、真实输入、输出、思考及接口返回的可选分项，不复制提示词正文。旧校准样本在备份后失效，旧待续任务中按估值强制换窗的标记同步失效，正文草稿和已完成工具保留，等待新请求。剧情和已有的逐次用量不变，不从日志补造关联。详细日志清理保留观测，删除所属会话／工作区时一并删除；取消和审核拒绝不抹去已发生的用量；被改写的请求快照不能驱动当前剧情窗口。字段见 [Token 计数](../guides/token-counting.md#查看实测与完整预览)。
- **17**：新增原著文件绑定、调查者、独立进度、消息队列和详细记录表（设置 version 15 同步增加配置，见下）。个人数据包 version 7 支持可选携带原著文件和暂停的调查进度。已有对话默认未开启。授权、分支、恢复与接口见[资料调查](../design/novel-research.md)。
- **18**：为每个原著调查者保存 `auto_destroy`，旧记录迁移为 `false`，保持手动生命周期。个人数据包 version 8 携带该配置，旧包缺少时按 `false` 导入。只有最终报告可靠送达后才按选项自动销毁；全局设置格式、工具总说明和主 agent 提示词不变。
- **19**：新增消息的 `story_segments` 与回合的 `draft_breaks`，保存剧情模型多段正文的原文、显示及流式预览边界，正文自身只含连接空行。边界使用 UTF-16 偏移，最多两处；个人数据包 version 9 导入时检查边界与正文一致，旧消息和旧包使用空数组。`story_progress` version 5 在 `story.breaks` 保存已接收段边界，段数由边界推导，v1–4 的旧进度正文视为一段。完整回复仍共用一条消息和同一个 H，剧情快照格式仍为 version 4。
- **20**：引入普通剧情模型的交互图形契约。成功的 `present_ui` 调用参数随原有工具轨迹保存，没有独立的可变副本。已有剧情对话的 `disabled_tools` 显式加入该工具（其他选择保留），新对话也默认关闭。个人数据包 version 10 校验成功图形的组件、动作和引用结构，旧包导入时默认关闭该能力。全局设置不变。
- **21**：消息新增 `ooc` 列，独立保存用户的场外发言。程序在故事模型文本后附加带 `<ooc>` 标签的场外原文，形成 `model`。已有消息和旧数据包的独立场外字段为空，正文中手写的标签按原样保留。个人数据包 version 11 携带该字段。导入时，非空场外发言必须属于用户消息，并与模型消息末尾的场外原文一致。
- **22**。原著绑定保存参谋 agent 的独立授权与修订，已有对话默认关闭。个人数据包仍可携带原著，导入后由用户开启参谋授权。
- **23**：制卡对话新增 `cache_follow` 列，保存本地断点的策略与跟进起点。已有对话默认固定前缀。
- **24** 为剧情对话增加默认关闭的 `unicode_story`，`story_progress` version 6 保存原回合的交付格式。个人数据包 version 13 保存对话开关和对应的工具轨迹，旧包缺省时关闭。
- **25** 将 `story_progress` 升为 version 7，以 `story_format` 记录本轮使用的 `plain`、`unicode_full` 或 `unicode_partial`。旧进度按原版本确定格式，已有正文和工具轨迹保留。个人数据包仍为 version 13，保存会话开关和已提交的工具轨迹。

- **26** 移除无法转换到现有格式的旧故事及其附属记录，并保留升级前的备份。
- **27** 将剧情快照、恢复进度、工具轨迹、消息适配和回合事件转为当前字段名。已提交正文、用户变量、待审核草稿和已完成工具结果随迁移保留。
- **28** 将角色卡、世界书及其结构化副本的条目用途迁移至 `extensions.terrarium.purpose`。涵盖分支快照、回合草稿、续接进度与制卡撤销。个人数据包 version 16 使用同一格式。
- **29** 增加 `sessions.media_config/media_sequence`、`media_assets`、`media_jobs` 和手动操作幂等记录 `media_actions`。配音、生图及看图默认关闭，设置均可在会话中查看和修改。`story_progress` version 8 保存生成计划、新声线和待提供图片；兼容旧进度。个人数据包 version 17 携带媒体及引用，导入／分支复制不会自动重发付费任务。全局设置仍为 version 22。
- **30** 将原有文件调查的委派授权用作资料调查者总开关，并为分支和任务保存独立的联网权限与调查指引。允许无文件的联网任务。迁移保留任务、轨迹和消息队列，旧调查的联网权限关闭。个人数据包 version 18 携带这些配置及任务记录，导入后任务保持暂停。
- **31** 保存游玩当前段缓存统计和空闲计时。已有对话从升级后的实际请求起算，历史用量记录保留。整库备份携带当前统计，个人数据包导入的对话独立起算。
- **32** 保存游玩任务开始时的累计用量，供运行期间的预览读取。空白剧情进度保留初始窗口标识，首轮失败后重新打开或续接可继续累计。升级保留已有统计，个人数据包格式仍为 version 18。
- **33** 制卡消息保存发送时的编辑目标和显式流程，末次写入失败可记为待续。编辑上下文版本 5 增加文档定位记录，旧草稿和未提交进度保留。已有消息不推测目标或流程。

- **34** 剧情动态世界书支持按编号展开。新的召回工具交流一次保存预览未展示的原文，按编号补读时还原召回当时的全文，随分支、个人数据包 version 19 及待续进度 version 9 保存。旧工具结果保留当时返回的全文。
- **35** 世界书全文读取记录按条目保存原注入锚点。手动移除后，模型上下文会跳过来自该次注入的全文。旧读取记录使用分支快照与主动召回次序定位来源。个人数据包 version 20 和回合续接进度 version 10 携带锚点信息。
- **36** 会话媒体设置、待提交计划和任务记录支持各渠道的声线与生成参数。旧 OpenAI 任务沿用原协议、声音、尺寸与 PNG 输出。个人数据包 version 21 和回合续接进度 version 11 携带新协议参数。
- **37** 新故事可引用设置文档 version 24 的全局媒体参数，仅在故事中保存授权和声线。旧故事及旧包中的完整媒体配置继续独立生效。个人数据包 version 22 携带来源，已登记任务保留原参数。
- **38** 设置文档 version 25 将配音和生图分别按普通 API 与 OpenAI 官方保存，保留原连接、原生协议和额度。已保存角色声线归入升级时的故事、剧情模型和配音模型组合。个人数据包 version 23 携带分用途来源与角色表，续接进度 version 12 保存配音计划所属剧情模型。
- **39** 修复 schema 38 中导入早期故事包时误写旧列默认值的记录。这些故事恢复为跟随全局，媒体授权保持关闭。续接进度 version 13 记录尚在补执行的旧配音批次，再次中断后可继续使用当时选定的音色。
- **40** 自动世界书的入选记录按回合保存，在提交事务中归属回复版本。升级从尚存的入选事件恢复，旧消息保留快照读取。个人数据包 version 24 携带已提交的入选记录和原剧情锚点。

设置文档 **version 26** 与个人数据包 **version 25** 支持在 Gemini 3.8 和 OpenAI Realtime 的默认音色、角色绑定和配音任务中保存云端音色 ID。这些 ID 沿用音色字符串字段，升级保留已有选择。旧版读取器可按版本识别其无法校验的音色配置。

设置文档 **version 27** 与个人数据包 **version 26** 支持保存 Chat Completions 生图配置及其任务。升级沿用原有媒体协议。

### 设置格式变更

- **22 → 23** 游玩设置增加 `story.lore_preview_chars`，默认 120 个 Unicode 字符，可设 1–2,000,000（含两端）。常驻条目始终全文。

- **2 → 3**：`shared.card_editor`、`shared.card_editor_config` 原样移到 `shared.card_author`、`shared.card_author_config`；设置导入同样接受旧键名。旧编辑对话保留原身份。
- **5 → 6**：为 `shared.kemini` 增加 `clear_prompt`、`info_prompt`、`transport_prompt`、`tool_description` 和 `content_description`，新增字段写入可见默认值，已有文本与开关原样保留。Kemini 文本统一从共享设置读取，支持本页撤回及导入导出；不再另外返回只读的 `kemini_protocol`。
- **6 → 7**：增加 `roleplay_prompt`、`content_prompt`、`output_prompt`，分别对应角色扮演追加要求、正文约束和末尾输出提示。只有 CLEAR、资料起始和自检文本完全等于旧默认值时，才替换其中的独立边界标签、Interleaving 包装和相关引用；小说填充、全部自定义文本和开关原样保留。十三项文本以数据库中的保存值为准。
- **7 → 8**：补齐 `story.tools.chapter_tokens` 和 `chapters_total_tokens` 的默认值。
- **8 → 9**：允许显式的 Android `cpu` / `q8_0` GGUF 配置。已有 CUDA 模型、量化、设备、版本和其他设置原样保留；用户在本地模型页选择手机配置后才写入新值。
- **9 → 10**：增加 `story.tools.max_duration_seconds`，整数秒，范围 30–7200（含两端），默认 900。游玩设置修订号增加，其他设置与提示词原样保留。字段在“工具与记忆”页可编辑，参与范围保存、撤回和个人设置导入导出；运行中的回合继续使用开始时的快照。时限覆盖模型、工具、自动整理和正文回调，不含等待用户审核；显式继续时取得新时限。
- **10 → 11**：给 `retrieval` 补齐 `index_timeout`（单条向量索引超时，整数秒，范围 1–3600，默认 60）。重复迁移保留用户已调整的时限。
- **11 → 12**：把 `shared.local_models.{embedding,reranker}.device` 的 Android Q8_0 设备选择扩展为 `cpu`、`opencl:0`、`hexagon:0`。已有设备值和其他配置原样保留，导入导出使用相同格式。
- **12 → 13**：把 `shared.kemini.clear_prompt`、`thinking_prompt`、`output_prompt` 的保存值迁移到 Interleaving 输出格式，默认值和自定义值都迁移：保留首尾空白、原有换行和其他自定义内容，优先替换可定位的旧示例和引用；找不到可识别的格式段时，把完整的对应原文模块追加到同一可编辑字段。只对实际修改过的 shared 范围增加一次修订号，之后读取不再迁移。数据库仍为 schema 13，不修改历史回复，不增加开关。
- **14 → 15**：增加 `story.novel_research`。普通 API 和 OpenAI 官方的模型字段默认 `null`，跟随当前对话的模型。额度按[资料调查默认值](../design/novel-research.md#模型额度与压缩)补齐。

- **15 → 16**：增加官方渠道的 `openai_api_key` 认证方式，固定 Responses 与 `/v1`。已有提供商、角色配置和渠道归属保持不变，不自动迁移旧 API Key 连接。
- **17 → 18**：移除 `shared.reply_locate_start`（回复完成后定位到正文开头）。生成期间改为跟随到正文开头即停，完成时不再跳转，见[阅读、编辑与分支](../guides/story-history.md#定位历史)。保存值存在时删除并使 shared 修订号加一；设置导入同样丢弃该键。

- **16 → 17**：缓存时效增加 `12h`。已有时效、缓存 JSON 和思考档位原样保留；官方 API 在请求前拒绝不适用或冲突的配置，界面提示修正，不自动替换。官方 API 工具传输改用扁平函数列表，旧云端状态按来源协议变化重建，本地正文与工具记录保留。
- **18 → 19**：主提示词迁移至各自第 1 项并启用，保留显式空值。内置默认文本的更新规则作用于第 1 项。个人数据包 version 12 携带完整的选项设置。
- **19 → 20** 增加 `shared.unicode_story_enabled`，默认 `false`。共享设置修订号加一。

- **20 → 21**：清理过期的检索与实验字段。
- **21 → 22**：游玩设置、对应修订号使用 `story`，请求前缀调试开关使用 `shared.debug_story_prefix`。

以上升级都在写入前做整库备份。

### 其他配置迁移

- 旧平铺设置按当时的有效连接恢复剧情模型和制卡 agent 的模型，已有提供商、密钥和共享设置继续使用。
- 旧提供商中的协议和路径迁移到引用它的模型，模型独立保存接入方式。
- 缺少缓存模式的配置迁移后，正文使用本地控制，制卡 agent 使用云端策略；已有会话的手动断点及跟进参数保留。
- 参谋 agent 的主提示词第 1 项与已知旧出厂文本完全相同时，更新为当前出厂规则。制卡 agent 的所有选项均执行这项检查，提示词中的独立旧游玩资源名也会转为 `story.md`。有变更时，`shared` 修订号增加一次。

迁移覆盖已实现的设置和数据库格式转换，历史行为按对应版本记录核对。

## 实现与验证

| 入口 | 职责 |
|---|---|
| [core/settings.py](../../src/tavern/core/settings.py) | 设置模型、迁移、运行时投影 |
| [application/settings_api.py](../../src/tavern/application/settings_api.py) | 页面目录、修订校验、公开视图和撤回 |
| [storage/store.py](../../src/tavern/storage/store.py) | 数据库版本检查、备份与持久化 |
| [useSettingsEditor.ts](../../web/composables/useSettingsEditor.ts) | 保存队列、草稿与响应合并 |
| [test_settings_document.py](../../tests/backend/test_settings_document.py) | 设置范围、迁移、密钥保护与并发行为 |
