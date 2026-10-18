# 参谋 agent 设计

Advisor 是两种故事模式共用的独立咨询运行时，入口在 `advisor/`。复用模型传输、消息适配、分词器、只读故事服务和检索准入，不进入 Writer／Tracker 编排，也不复用 Full Agent 的写入草稿。

## 配置与来源

配置位于 `shared.advisor`，包括 `model`、`official_model`、`system_prompt`、`recent_exchanges`、`keep_recent`、`auto_compact`、`context_strategy`、`max_steps` 和 `web`。默认值及操作语义见[使用指南](../guides/advisor.md)。故事覆盖仅保存参谋 agent 模型选择、近期交流数及上下文方式，显式 null 清除覆盖；界面展示生效来源。

`shared.advisor.web.mode` 与 `shared.card_editor_config.web.mode` 分别控制两个 Agent 的 `off/auto/local` 权限，服务设置集中在 `shared.web`。`web/` 模块承载 Exa Streamable HTTP MCP、Tavily Search/Extract 及旧搜索实现。旧参谋配置迁移到共享设置前备份，保留原开关、端点和超时；默认服务顺序升级为 Exa、Tavily、SearXNG、DuckDuckGo。密钥脱敏、清除、页撤回和选择性导出沿用设置 API。

官方自动模式在公开 Responses 请求中将原生 `web_search` 与 `tavern` namespace 并列，并请求 `web_search_call.action.sources`；本地搜索函数同时隐藏。只允许 Advisor／角色卡编辑 agent 主请求启用，维护请求与其他 Agent 不启用。搜索专属故障最多重试当前模型请求一次，原有已执行工具不重放，未完成文本通过 `response_restarted` 撤回；模式降级随未完成进度保存。账号、额度和断流错误不冒充搜索故障。

云端输出项和引用注解完整保存在原生附件中，工具续答按原模型／账号绑定回放；来源链接另作为可移植 `web_sources` 保存并展示。`web_activity` 记录服务、鉴权模式、状态与降级原因，不包含密钥。本地搜索统一限量来源与摘要，网页按字符分页；每次实际返回的片段完整持久化并送往模型，不在传输阶段再次截断。服务错误、上游截断、返回量限制与合法零命中分别标记，见[联网工具协议](../reference/web-tools.md)。

运行前校验故事的不可变渠道。参谋 agent 角色使用独立模型配置，不从 Writer、Tracker 或 Full Agent 推断供应商。模型连接检查、能力失效、提示词保存、设置页撤回及消息适配均接入共享设置机制。

## 上下文与投影

固定前缀是参谋 agent 系统提示词、角色与用户资料、常驻世界书。动态背景包含当前偏好、模式对应记忆及按完整交流选择的近期剧情；锚定于所选交流的世界书保持完整。主故事未来规划与长期笔记默认按需读取。

一轮开始时冻结当前分支、剧情与状态，工具续答读取同一版本。当前用户咨询引用为 Q1；剧情使用 H 引用，世界书 L 引用，Half Agent 记忆 R 引用，旧咨询 C 引用。真实存储身份、来源指纹和版本仅供程序定位与冲突校验，业务文本经 ModelView 投影；保存时恢复原始内容。云端压缩和续接保留可逆编号映射。

消息适配提供 `advisor_reference`、`advisor_story`、`advisor_summary`、`advisor_notice`。预览和实际运行共用编译器，最终投影后使用分词器计数。已选内部资料与工具返回片段完整保存并送入模型，超量时提示。

参谋 agent 咨询历史与剧情分开存储。咨询摘要只承接按完整交流退出的旧咨询，最近咨询与当前未完成工具交换保持原文。主故事追加新剧情不会使旧咨询摘要成为当前事实；修改或撤销摘要依据、模型/账号/关键规则变更时从原文重建。云端 checkpoint 仅属于参谋 agent，不读取主 Agent 的不透明状态。整理中断保存已处理的完整批次，可续接。

## 工具契约

相同能力复用主 Agent 名称与参数模型；不共享可修改的草稿对象。

| 工具 | 参数与作用 |
|---|---|
| `search_history` / `read_history` | 沿用 Full Agent 的 query/limit、ref；搜索当前分支并完整读取 H 原文 |
| `recall_lore` | query；沿用准入，在独立快照中召回完整相关正文，不改变故事冷却和注入 |
| `find_lore_for_edit` / `read_lore_entry` | 沿用 query/limit、ref；定位包括停用项的普通条目并读取，参谋 agent 没有编辑能力 |
| `read_character` | 沿用 ref 字段；读取允许的人物设定字段 |
| `read_state` | 沿用 Full Agent 的 scope/path（JSON Pointer）；读取已提交状态 |
| `search_notes` / `read_note` | Full Agent 使用，沿用 query/limit、key，保留来源与失效状态 |
| `search_memory` | Half Agent 使用，沿用 Tracker 的 query，返回完整匹配记忆及来源状态 |
| `read_story_context` | field 为 handoff/plot/turn_preference/persona/title；只读，缺失不生成 |
| `search_consultation` / `read_consultation` | query/limit、ref；查阅当前分支旧咨询 |
| `web_search` / `read_webpage` | 搜索使用 query/limit/可选 objective；网页使用 url/offset/limit/可选 service。返回有界摘要或提取片段、续读位置、截断状态、实际服务与尝试记录，见[联网工具协议](../reference/web-tools.md) |
| `offer_inputs` | options 为 1–6 条完整输入；在回复下方显示为可点击选项，点击只填入主对话输入框 |
| `offer_titles` | titles 为 1–6 个候选标题（各 1–100 字符）、request_ref=Q1；仅用户明确要求改名或起标题时调用，点击后才修改对话标题 |
| `update_turn_preference` | action 为 append/replace/clear，content、request_ref=Q1；仅明确用户指令授权 |

两种选项工具不写入任何数据：已持久化的调用参数即界面展示的选项，执行失败的调用不显示；填入输入框与改名均由用户点击触发，改名沿用会话标题接口及修订检查。不注册故事写入、脚本执行或主 Agent 换窗工具。工具 allowlist 在执行时再次检查。用户意图由模型根据真实当前提问判断，Runtime 校验引用、字段、故事修订和互斥状态，不通过关键词猜测授权。

## 存储、事务与恢复

schema 4 新增 `advisor_conversations`、`advisor_exchanges`、`advisor_runs`、`advisor_events`。旧数据库打开前只读校验并备份；旧版本程序拒绝未知格式。已完成咨询包含用户输入、最终回复、完整配对工具轨迹、来源锚点和指纹；派生摘要与云端状态独立保存。

运行进度保存冻结背景、待执行工具、已配对结果、偏好草稿和编号映射。完整响应先保存，再逐项执行与保存；重试跳过已配对调用。操作 ID 在故事内幂等，SSE 按序号重连观察，不负责提交。取消与重启不丢失待续进度。

偏好修改先进入咨询草稿。最终提交使用 SQLite 写事务，再检查故事修订、偏好原值以及运行/待审核/上下文整理互斥状态，成功后一起保存偏好与咨询结果。冲突不部分提交、不覆盖人工编辑。咨询成功不会增加剧情消息或修改快照。

每个故事只有一份临时咨询，由 `advisor_conversations.session` 主键约束；不提供归档或多会话列表。清空须在任务停止后执行，同一事务内删除交流、任务和级联事件，重置摘要及云端上下文并递增修订号；保留故事级设置及已提交偏好，旧请求因修订冲突不能复活记录。分支和故事导入导出只保留设置，不复制或恢复咨询；完整目录备份仍保留当时的数据快照与待续任务。删除故事通过归属级联清理。诊断沿用日志保留和容量设置，保护运行与待续记录，当前咨询由用户整体清空。

## API 与界面

`/api/sessions/{sid}/advisor` 提供只读状态；其下 `options`、`preview`、`runs`、`discard`、`clear` 分别管理故事覆盖、只读预览、任务提交、丢弃进度与清空当前咨询。`/api/advisor-runs/{rid}` 提供任务状态，其下 `events`、`cancel` 提供 SSE 与取消。

前端 `AdvisorPanel` 和 `useAdvisor` 分别负责界面与请求生命周期。面板首次打开后在当前故事内保持观察，隐藏时仍可同步偏好；切换故事或离开故事页关闭观察连接，异步结果不得覆盖新视图。上下文预览使用原生 modal dialog 保证焦点和背景隔离；小屏沿用右侧抽屉，页面根节点不滚动。
