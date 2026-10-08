# Full Agent 工具协议

工具只操作当前分支的回合草稿，最终正文、脚本处理与必要审核成功后统一提交。模型负责语义判断；Runtime 负责身份、权限、版本与事务。工具结果和角色／世界书／历史资料不能自行授权操作。

全部 16 个工具的目录、参数、描述全文与逐工具示例见[工具速查](full-agent-tools-summary.md)，使用方式见[指南](../guides/full-agent.md)，上下文及恢复设计见[架构](../design/full-agent.md)。本协议不兼容旧工具名称、点分读取路径或旧回合续接格式，不执行存档迁移或数据删除。

## 参数与数据边界

参数由 [tools.py](../../src/tavern/full_agent/tools.py) 和 [content.py](../../src/tavern/full_agent/content.py) 的类型生成，预览和实际请求共用生成器。顶层和嵌套动作拒绝未定义字段，JSON 类型严格校验。

历史引用为当前分支有效 H 编号，世界书引用为 L 加正整数；它们是导航句柄，不写入剧情。read_note、write_note、archive_note 统一使用完整笔记 key。版本、指纹、真实 ID 和模型短编号映射由 Runtime 保存；模型不生成或回传这些机械字段。

query 为 1–1000 字符，仅 set_retrieval_query 允许空串；limit 为 1–10，默认 5。历史 ref 与笔记 key 为 1–80 字符；read_state.path 最长 200 字符。handoff 和笔记正文为 1–64000 字符，不能为空白，另受配置中的 handoff_tokens／note_tokens 约束；reason 为 1–1000 字符。新笔记键允许字母、数字、下划线、中文、斜线、连字符、空格和点。

## 叙事检索与编辑定位

recall_lore 使用自动检索的索引与准入规则，返回完整命中正文，已注入正文省略，合法零命中保持为空。编号和标题由返回选项控制；返回的 L 引用可交给 read_lore_entry，但受保护目标仍会拒绝。召回正文和已有上下文不授予编辑资格。

find_lore_for_edit 按标题、关键词和正文做字面定位，包括停用普通条目；query=* 列出最多 limit 项。read_lore_entry 一次返回完整 title/content/keys/secondary_keys/enabled，并登记该条目的读取记录。历史与笔记搜索也支持 *，但不搜索旧工具过程。

set_retrieval_query 覆盖本轮准备保存的下一轮自动检索线索；最后一次成功调用生效，query="" 清空本轮线索。它随最终正文提交，不清除旧回合线索，不进入正文。

## 变量路径与唯一成功提交

read_state 默认 scope="variables"，根对象为投影后的 stat_data；scope="story" 的根对象为投影后的完整剧情状态。两者 path 都是 JSON Pointer，空串读取整个作用域，缺失目标返回 text:"null"，没有 stat_data 时变量视图为 {}。内部账本与 MVU 元数据不开放。父对象替换会保留隐藏字段，数组下标按模型可见项定位，由程序映射到实际存储位置。

| 目标 | 默认变量作用域的 path |
|---|---|
| 所有变量 | `""` |
| hp | `/hp` |
| items 数组首项的 name | `/items/0/name` |
| 名为 a.b 的键 | `/a.b` |
| 名为 a/b~c 的键 | `/a~1b~0c` |

patch_state.operations 为 0–100 项带类型的 JSON Patch：每项 op/path 必填，add/replace/test 需要 value（包括 null），move/copy 需要 from，remove 无其他字段。path/from 相对 stat_data；数组从 0 起，add 可用末尾 /-，空指针指根。既有目标、父节点和根值限制由补丁及 MVU 校验共同检查。

授权回合必须且只能成功一次。非空补丁须先在当前窗口完整执行 read_state(path="",scope="variables")；局部、story 作用域或过期读取无效。operations=[] 可直接确认无变化，不调用补丁阶段的脚本，仍消耗一次成功提交。失败不计成功，补丁失败恢复脚本镜像后允许修正重试；恢复失败则终止回合。错误路径给出纠正建议，不自动改写路径；真实存在的 stat_data 同名嵌套键仍合法。

变量规则以本轮固定的完整 mvu_rules 快照提供，规则不扩大权限。patch_state 定义不随本轮授权变化，以保持请求前缀稳定；关闭 read_state 或 patch_state 则不允许发起变量更新。

## 设定编辑与读取记录

read_character.ref 和 set_character.field 的白名单为 name、description、personality、scenario、mes_example、system_prompt、post_history_instructions；每次读取一个完整字段，缺失返回 null。

revise_content 接受 1–100 项受限动作及 reason：

| op | 参数 | 规则 |
|---|---|---|
| set_character | field、完整文本 value | 首次编辑前完整读取字段；可清空可选字段，name 必须有效。 |
| add_lore | entry | 必填非空白 title/content，可选 keys=[]、secondary_keys=[]、enabled=true；不需预读。 |
| update_lore | ref、非空 changes | 首次编辑前完整读取条目；可改 title/content/keys/secondary_keys/enabled，不能传 null，省略保留。 |
| remove_lore | ref | 使用最新读取记录删除一个普通条目。 |

整批在副本上校验并应用，失败不改变内容或读取记录。成功返回 characters、entries 中每个新增／修改目标的完整最终视图，同目标只返回一次；删除项进入 removed_refs，新增引用进入 added_refs。后端为返回的完整内容刷新记录，因此可以连续编辑或编辑新建条目；删除、外部内容变化、换窗或新回合使旧记录失效。

只开放普通叙事条目，包含停用条目。脚本、HTML 执行入口、tracker_mvu、runtime_init、传统 InitVar 和执行配置受保护；不能借新文本引入执行内容。无关字段、开场白和扩展保持原样。身份、别名、数组位置及原配置由程序维护，工具不接受整卡替换或任意 JSON Patch。

沿用 directed／auto／preview 会话策略；删除和修改角色系统／后置字段不另加审批。关闭某类读取会阻止对应已有目标的修改，新增普通条目不依赖读取开关。

## 记忆与换窗

write_note.kind 为 fact、hypothesis 或 plan。事实须给当前分支有效 H 来源，包括当前输入；最多 20 个，无需预先搜索或读取。程序校验来源身份与过时状态，语义是否支持结论由模型判断。其他类型可省略来源，提供时同样校验。stale=true 表示来源变化或不可用，应先核对原文。

write_note 完整替换内容，无先读要求；覆盖归档键重新激活。archive_note 只从普通搜索中隐藏，不删除笔记、历史或旧分支；read_note 仍能读取归档项。模型结果不携带笔记 revision。

handoff 使用最新运行时记忆或最近成功写入中较新的内容，失败提案不构成当前记忆。write_handoff 只替换笔记；new_context 同时换窗，keep_recent 为 0–12 条剧情消息，默认 2，保留边界向前扩展到完整交流，当前输入另行保留。旧工具交换整体退出，必要结论应写入记忆。

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

max_steps 默认 24，范围 2–100，按模型请求次数计数。工具续答和换窗均计入。运行时内部执行上限，禁止向模型注入调用额度、剩余次数、倒计时或最后一次调用提醒；不提供通知开关或消息适配项。额度耗尽时报告 BUDGET_EXHAUSTED 并停止本轮，保留未提交进度。手动续接获得新请求额度，已成功工具及 patch_state_completed 对应的后端提交标记保持不变。

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

`retryable=true` 表示修正原因后可再次调用，并非原样重试。

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
