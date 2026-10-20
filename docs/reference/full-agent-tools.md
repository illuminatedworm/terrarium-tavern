# Full Agent 工具协议

工具只操作当前分支的回合草稿，最终正文、脚本处理与必要审核成功后统一提交。模型负责语义判断；Runtime 负责身份、权限、版本与事务。工具结果和角色／世界书／历史资料不能自行授权操作。

全部 17 个工具的目录、参数及源码示例见[工具速查](full-agent-tools-summary.md)，使用方式见[指南](../guides/full-agent.md)，上下文及恢复设计见[架构](../design/full-agent.md)。工具调用使用当前目录和 JSON Pointer 路径，存档迁移由存储层处理。

## 参数与数据边界

剧情规划 `plot` 独立于 note，使用 `write_plot({"content":"完整阶段大纲"})` 写入。其唯一开关为会话的 `plot_enabled`，默认关闭；工具页不能独立禁用它。正文为 1–64000 字符且非空白，另受 `plot_tokens` 限制，默认 4096 tokens。当前完整版本通过 `<tavern_plot>` 提供，无需预读或来源引用；成功只返回 `saved_to_draft:true`。开启且大纲为空时，运行时拒绝提前输出的正文，要求先成功建纲；后续不要求每轮写入。

玩家接口 `PUT /api/sessions/{sid}/full-agent/plot` 接收 `expected_revision` 和可选 `enabled`／`content`，至少提供一项；空字符串清空大纲。开关可在空白故事修改，正文须已有剧情节点。关闭保留内容并暂停使用；修改与运行、待审核和上下文整理互斥。手动编辑后的大纲仍可由模型调整。

参数由 [tools.py](../../src/tavern/full_agent/tools.py) 和 [content.py](../../src/tavern/full_agent/content.py) 的类型生成，预览和实际请求共用生成器。顶层和嵌套动作拒绝未定义字段，JSON 类型严格校验。

历史引用为当前分支有效 H 编号：H52 是当前分支第 52 条消息，开场白为 H1，当前用户输入按将要占据的位置编号；与 Half Agent Tracker、参谋 agent 和剧情界面显示的编号同一定义。世界书引用为 L 加正整数；它们是导航句柄，不写入剧情。read_note、write_note、archive_note 统一使用完整笔记 key。版本、指纹、真实 ID 和模型短编号映射由 Runtime 保存；模型不生成或回传这些机械字段。

query 为 1–1000 字符，仅 set_retrieval_query 允许空串；limit 为 1–10，默认 5。历史 ref 与笔记 key 为 1–80 字符；read_history 的 ref 可为单条 H 或闭区间 `H52-H58`，区间最多 8 条、两端都须在当前分支。read_state.path 最长 200 字符。笔记正文为 1–64000 字符，不能为空白，另受 note_tokens 约束；handoff 的限额见下文；comment 为必填的简短操作备注，1–1000 字符。新笔记键允许字母、数字、下划线、中文、斜线、连字符、空格和点。

patch_state 和 revise_content 的备注参数统一使用 comment，替代旧参数 reason；旧调用记录保留原样，新调用传 reason 会返回参数错误及当前参数定义。

## 叙事检索与编辑定位

recall_lore 使用自动检索的索引与准入规则，返回完整命中正文，已注入正文省略，合法零命中保持为空。编号和标题由返回选项控制；返回的 L 引用可交给 read_lore_entry，但受保护目标仍会拒绝。召回或自动注入的完整当前正文可以作为修改依据，修改权限仍由本轮授权决定。

find_lore_for_edit 按标题、关键词和正文做字面定位，包括停用普通条目；query=* 列出最多 limit 项。read_lore_entry 一次返回完整 title/content/keys/secondary_keys/enabled，并登记该条目的读取记录。历史与笔记搜索也支持 *，但不搜索旧工具过程。

set_retrieval_query 覆盖本轮准备保存的下一轮自动检索线索；最后一次成功调用生效，query="" 清空本轮线索。它随最终正文提交，不清除旧回合线索，不进入正文。成功结果只返回 saved_to_draft 确认，不回传刚写入的 query 文本。

## 变量路径与唯一成功提交

read_state 默认 scope="mvu"，根对象为投影后的 stat_data；scope="full" 读取全部模型可见运行时状态，包含 stat_data；scope="other" 读取同一状态中除顶层 stat_data 之外的字段。path 相对于所选范围，使用 JSON Pointer，空串读取整个范围，缺失路径返回 text:"null"，没有 stat_data 时 mvu 返回 {}。所有范围都过滤内部账本与 MVU 元数据，full 不开放内部数据。只有完整 mvu 或 full 中的完整 stat_data 可作为非空变量补丁的上下文依据，other 供读取其他运行时字段。

旧工具调用中的 variables／story 分别按 mvu／full 兼容解析及回放，新工具目录只提供 mvu／full／other。父对象替换会保留隐藏字段，数组下标按模型可见项定位，由程序映射到实际存储位置。

| 目标 | 默认变量作用域的 path |
|---|---|
| 所有变量 | `""` |
| hp | `/hp` |
| items 数组首项的 name | `/items/0/name` |
| 名为 a.b 的键 | `/a.b` |
| 名为 a/b~c 的键 | `/a~1b~0c` |

patch_state.operations 为 0–100 项带类型的 JSON Patch：每项 op/path 必填，add/replace/test 需要 value（包括 null），move/copy 需要 from，remove 无其他字段。path/from 相对 stat_data；数组从 0 起，add 可用末尾 /-，空指针指根。既有目标、父节点和根值限制由补丁及 MVU 校验共同检查。

授权回合必须且只能成功一次。非空补丁需要上下文中的完整当前变量；完整 mvu／stat_data 读取、工具参数中的完整根值，或完整读取加成功补丁可准确还原当前值时均有效。完整 full 读取含 stat_data 时也有效，局部变量、过期值及无法还原的脚本变化不算。缺少当前版本时先 read_state(path="",scope="mvu")。operations=[] 可直接确认无变化，不调用补丁阶段的脚本，仍消耗一次成功提交。失败不计成功，补丁失败恢复脚本镜像后允许修正重试；恢复失败则终止回合。错误路径给出纠正建议，不自动改写路径；真实存在的 stat_data 同名嵌套键仍合法。

变量规则以本轮固定的完整 mvu_rules 快照提供，规则不扩大权限。patch_state 定义不随本轮授权变化，以保持请求前缀稳定；关闭 patch_state 不允许发起变量更新。关闭 read_state 不连带禁用 patch_state，已有完整当前版本或提交空补丁时仍可执行。

## 设定编辑与读取记录

read_character.ref 和 set_character.field 的白名单为 name、description、personality、scenario、mes_example、system_prompt、post_history_instructions；每次读取一个完整字段，缺失返回 null。

revise_content 接受 1–100 项受限动作及 comment：

| op | 参数 | 规则 |
|---|---|---|
| set_character | field、完整文本 value | 当前上下文没有完整最新字段时先读取；可清空可选字段，name 必须有效。 |
| add_lore | entry | 必填非空白 title/content，可选 keys=[]、secondary_keys=[]、enabled=true；不需预读。 |
| update_lore | ref、非空 changes | 当前上下文没有完整最新条目正文时先读取；可改 title/content/keys/secondary_keys/enabled，不能传 null，省略保留。 |
| remove_lore | ref | 当前上下文须有该普通条目的完整最新正文，否则先读取。 |

整批在副本上校验并应用，失败不改变内容或读取记录。成功返回 characters、entries 中每个新增／修改目标的完整最终视图，同目标只返回一次；删除项进入 removed_refs，新增引用进入 added_refs。自动注入、完整工具结果、模型自己在工具参数中提供的完整当前文本均算修改依据；同窗口跨回合可沿用。目标改变、删除或原文退出上下文后重新核对，没有当前版本才须读取。读取工具关闭不阻止上下文依据充分的修改。

只开放普通叙事条目，包含停用条目。脚本、HTML 执行入口、tracker_mvu、传统 InitVar 和执行配置受保护；不能借新文本引入执行内容。无关字段、开场白和扩展保持原样。身份、别名、数组位置及原配置由程序维护，工具不接受整卡替换或任意 JSON Patch。

本轮 `allow_content_edit` 授予设定写入权限，`automatic`／`review` 决定实际修改的保存方式。角色系统及后置字段沿用同一权限。读取与写入工具分别启停；已有目标的完整当前内容仍在上下文时可以修改，缺少依据时须先启用相应读取工具并补读。新增普通条目直接按参数和权限校验。

## 记忆与换窗

write_note.kind 为 fact、hypothesis 或 plan。事实须给当前分支有效 H 来源，包括当前输入；最多 20 个，无需预先搜索或读取。程序校验来源身份与过时状态，语义是否支持结论由模型判断。其他类型可省略来源，提供时同样校验。stale=true 表示来源变化或不可用，应先核对原文。

write_note 完整替换内容；覆盖已有键或 archive_note 归档前，当前完整正文须已在上下文，否则先 read_note。工具参数中的完整当前正文也算，成功写入后不必重复读取。新 key 无需读取，覆盖归档键重新激活。目录和搜索摘要不算完整正文。archive_note 只从普通搜索中隐藏，不删除笔记、历史或旧分支；read_note 仍能读取归档项。模型结果不携带笔记 revision。

handoff 使用最新运行时记忆或最近成功写入中较新的内容，失败提案不构成当前记忆。它由章节和当前交接组成，结构见[handoff 章节](../design/full-agent.md#handoff-章节)。new_context 的 `handoff` 可省略或为 `null`（默认），表示沿用当前交接，不改内容、版本及来源指纹；只有需要更新时才传入。传入时的结构与 write_handoff 参数相同：

- `chapters`：0–10 个按顺序执行的操作，C 编号指本次调用前的编号。`add` 通常只给标题和要点，起点自动接在上一章之后；省略 `through` 时，new_context 自动覆盖到最后一条退出消息，write_handoff 自动覆盖到最后一条已提交消息。只有主动语义分章时才用已知 H 填写 `through`，一次调用中省略终点的 add 应放在其他 add 之后。`revise` 改写一章的标题与要点，范围不变；`merge` 把 2–10 个相邻章节合并为一章，范围取并集。title 为 1–80 字符，每章 1–20 条要点。
- `current`：1–30 条要点，整体替换当前交接。
- 要点：`text` 1–2000 字符；`refs` 可省略、默认 `[]`，最多 4 个 H 引用或闭区间，章节引用须落在本章范围内；`kind` 为 fact（默认）或 inference。引用只用于有精确回查需要的已提交原文，不为文风、角色资料或当前输入拼凑 H；这些内容直接写入交接即可。assistant 正文不带位置标记，精确 H 从历史工具取得，不要求推算。
- 限额：当前交接 handoff_tokens（默认 2048），单章 chapter_tokens（默认 512），全部章节 chapters_total_tokens（默认 4096），均按渲染后的文本计数。章节总量超限时返回 CONTENT_LIMIT，提示合并最旧的相邻章节。
- new_context 先根据模型选择的 `keep_recent` 将保留边界扩展到完整交流，只要求章节覆盖最终保留起点之前的原文。保留的最近消息和当前输入不要求提前归档；全部历史保留时允许沿用空交接。实际退出范围未覆盖时返回 HANDOFF_COVERAGE，模型可补齐交接，或增加 keep_recent 保留尚未归档的交流。

成功结果返回 `chapters`：当前每章的 C 编号、标题和范围，供同一窗口后续调用 revise／merge。keep_recent 为 0–12 条剧情消息，默认 2，保留边界向前扩展到完整交流，当前输入另行保留。旧工具交换整体退出，必要结论应写入记忆。

例如 H1 为开场，H2–H7 为三组用户／AI 交流。选择 `keep_recent: 1` 时，最后一条 AI 回复会连同前一条用户消息一起保留，即 H6–H7；章节只需覆盖 H1–H5。现有章节已覆盖时可直接调用 `{"keep_recent":1}`，无需重写 handoff。

用户界面只读展示 handoff。手动换窗接口 `POST /api/sessions/{sid}/full-agent/new-context` 只接受 `expected_revision` 和 `keep_recent`（0–12 条剧情消息，默认 2，同样扩展到完整交流），复用已保存交接并检查退出范围覆盖；它不接收 handoff 正文，不调用模型或修改来源状态。

new_context 必须单独调用。任何包含它的多工具批次都在执行前整体拒绝，包括普通状态；强制换窗时其他工具和正文也被拒绝。普通工具批次按顺序逐项执行，单项失败不会回滚此前成功项；revise_content 的内部动作批次仍是原子的。

全局设置 `full_agent.tools.parallel_tool_calls` 默认 true，允许单次最多 8 个工具调用。关闭时，多调用批次中的未执行工具全部返回 PARALLEL_TOOL_CALLS_DISABLED，不产生新的草稿写入；模型应逐次调用。此限制由运行时统一校验，不依赖服务商是否支持或遵守原生参数。工具批次和内部动作批次不同，关闭此设置不限制 patch_state 或 revise_content 中合法的多项 operations。

## 返回格式与错误恢复

成功结果统一为：

```json
{"ok":true,"data":{"saved_to_draft":true}}
```

失败结果统一为：

```json
{"ok":false,"error":{"code":"READ_REQUIRED","message":"非空补丁前须完整读取最新变量","retryable":true,"next_action":"调用 read_state，使用默认作用域及空 path。","patch_state_completed":false}}
```

参数错误在顶层额外返回 expected_parameters；patch_state 的错误在 error 内额外返回 patch_state_completed，明确失败是否发生在已有一次成功提交之后。错误与结果都先做模型数据投影，不暴露真实身份和指纹；分类来自显式异常，不解析报错文案。

非法 JSON 或非法工具序列是 TOOL_PROTOCOL_INVALID，额度耗尽是 BUDGET_EXHAUSTED，提供商异常为 PROVIDER_ERROR，不完整回复为 PROVIDER_INCOMPLETE，脚本恢复失败为 SCRIPT_RESTORE_FAILED。它们是终止回合的诊断事件，不返回可重试工具结果；其他运行异常记录为 RUNTIME_FAILURE。

## 请求预算与草稿生命周期

`max_steps` 默认 24，范围 2–100，按工具循环的模型请求计数，包含工具续答和本地换窗；云端整理使用独立请求并另行记录用量。额度耗尽时返回 `BUDGET_EXHAUSTED` 并保留进度，显式继续获得新额度，保留成功工具及 `patch_state_completed` 的后端标记。

| 情况 | 正式剧情 | 草稿与恢复 |
|---|---|---|
| 正文、脚本处理及审核完成 | 原子提交 | 正文、笔记、变量、设定一起生效。 |
| 生成失败、预算耗尽、取消或中断 | 不变 | 保留进度；只能续接本会话最新且输入／授权一致的可恢复回合，成功工具不重跑。 |
| 等待审核 | 不变 | 整轮保持待提交。 |
| 明确拒绝审核，包括取消待审核回合 | 不变 | 清除 pending、draft、preview 和可续接进度，保留过程记录；续接接口拒绝恢复。 |
| 输入或授权改变 | 不变 | 以新请求重新开始，不复用旧草稿。 |

回合接口返回 resumable，前端仅在它为 true 且变量／设定授权均一致时尝试续接，否则发起新请求。该字段表示当前回合具有可恢复进度，实际提交时仍检查它是否为最新回合及版本是否匹配。

错误码表由 `tools/export_full_agent_tools.py` 生成；描述与示例的核对见[速查附录](full-agent-tools-summary.md#附录工具描述全文与最小示例)。

## 错误码（由源码生成）

`retryable=true` 表示处理 `next_action` 指出的原因后可再次调用。

| code | 可修正重试 | 默认 next_action |
|---|---|---|
| `UNKNOWN_TOOL` | 是 | 使用本轮工具目录中的名称。 |
| `PERMISSION_DENIED` | 否 | 遵守本轮授权；需要时由用户调整工具开关或授权。 |
| `INVALID_ARGUMENTS` | 是 | 按参数定义修正后重试。 |
| `INVALID_REFERENCE` | 是 | 重新搜索或读取当前分支中的有效引用。 |
| `READ_REQUIRED` | 是 | 在当前窗口完整读取最新目标后重试。 |
| `INVALID_PATH` | 是 | 使用 JSON Pointer；变量路径相对于 stat_data，如 /hp，不是 stat_data.hp 或 /stat_data/hp。不要自动删除真实同名键。 |
| `PATCH_VALIDATION_FAILED` | 是 | 核对最新变量与 MVU 规则，修正补丁后重试。 |
| `CONTENT_LIMIT` | 是 | 精简本次写入内容后重试；不能截断读取的原文。 |
| `PROTECTED_TARGET` | 否 | 仅修改工具公开的业务字段；程序内部字段与执行配置不可修改。 |
| `STATE_ALREADY_SUBMITTED` | 否 | 本轮变量已成功提交，不要重复调用 patch_state。 |
| `MIXED_CONTEXT_CALLS` | 是 | 本批次未执行任何工具；单独调用 new_context。 |
| `PARALLEL_TOOL_CALLS_DISABLED` | 是 | 每次回复只提出一个工具调用，等待结果后再继续。 |
| `CONTEXT_ROLLOVER_REQUIRED` | 是 | 单独调用 new_context 后继续本轮。 |
| `HANDOFF_COVERAGE` | 是 | 补齐实际退出范围的章节，或增大 keep_recent 保留未归档的完整交流，再单独调用 new_context。 |
