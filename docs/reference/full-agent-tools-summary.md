# Full Agent 工具速查

当前共 **16 个工具**。下表为中文摘要，文末附源码描述全文及每个工具的最小请求／返回示例。带 `=默认值` 的参数可省略，其余必填。

## 如何工作、为何这样设计

Full Agent 由同一个模型生成剧情、查资料和维护状态。后端把工具名称、说明和 JSON 参数结构交给模型；模型提出调用，运行时检查权限、参数及读取记录，执行后把结果送回模型，循环至输出最终正文。写入先进入当前分支的回合草稿，正文、脚本处理及必要审核完成后统一提交；工具成功不等于正式剧情已经保存。

设计目的是让模型按需取得完整资料，用交接笔记和长期笔记延续故事，并在授权下维护变量与设定。模型负责语义判断，程序负责身份定位、来源校验和一致性：H 引用定位剧情，L 引用定位世界书，真实 ID、版本和指纹留在后端。换窗降低上下文占用，原始剧情仍可查询；长期笔记按需读取，不自动全文注入。

## 完整工具列表

| 工具名 | 描述 | 参数 | 额外备注 |
|---|---|---|---|
| `recall_lore` | 为叙事召回完整世界书正文 | `query: string` | 共用自动检索的索引与准入规则；允许零命中，已注入正文省略。可选 L 引用可用于单条读取，召回不授予编辑资格。 |
| `find_lore_for_edit` | 定位可编辑的普通条目 | `query: string`；`limit: integer=5` | 字面匹配标题、关键词和正文，包含停用条目；`*` 列出条目；返回 L 引用、标题与定位摘要。 |
| `read_lore_entry` | 完整读取一个普通条目 | `ref: string`（如 `L1`） | 返回标题、正文、主／辅助关键词及启用状态，登记当前读取记录；保护目标不可读写。 |
| `search_history` | 搜索当前分支剧情原文 | `query: string`；`limit: integer=5` | 字面检索，返回 H 引用及可选摘要；`*` 列最近记录，不搜索旧工具过程。 |
| `read_history` | 完整读取一条剧情原文 | `ref: string`（如 `H1`） | 可读取当前用户输入，引用须在当前分支有效。 |
| `write_handoff` | 完整替换交接笔记 | `content: string` | 保留有效场景、关系、约束和未决事项；以最新运行时记忆或成功写入为基础。 |
| `new_context` | 保存交接笔记并换窗 | `content: string`；`keep_recent: integer=2` | 必须单独调用，混合批次全部不执行；保留 0–12 条最近剧情消息并扩展至完整交流，当前输入另保留；调用额度不重置。 |
| `search_notes` | 搜索有效长期笔记 | `query: string`；`limit: integer=5` | 按键和正文做字面检索；`*` 列笔记，不含归档项。 |
| `read_note` | 完整读取一条笔记 | `key: string` | 可读有效或归档项，可附类型、H 来源和 `stale`；过时来源应核对。 |
| `write_note` | 新增或完整替换笔记 | `key: string`；`content: string`；`kind: string`；`sources: string[]=[]` | kind 为 fact／hypothesis／plan；事实必有有效 H 来源，最多 20 个；无需预读，覆盖归档键会激活。 |
| `archive_note` | 归档笔记 | `key: string` | 普通搜索不再返回，仍可按键读取；不提供删除工具。 |
| `read_state` | 按 JSON Pointer 读取状态 | `path: string=""`；`scope: "variables"或"story"="variables"` | 默认相对 stat_data，如 `/hp`；空串读完整作用域。story 读取完整剧情状态的投影视图；只有完整 variables 读取授予非空补丁资格。 |
| `patch_state` | 提交变量补丁或确认无变化 | `operations: object[]`（0–100 项）；`reason: string` | 本轮授权时必须且只能成功一次，失败可修正重试；非空先完整读变量，空数组免读取且不触发补丁脚本回调。 |
| `read_character` | 完整读取一个人物字段 | `ref: string`（下述七字段之一） | 缺失返回 null；登记读取记录，含执行代码的字段受保护。 |
| `revise_content` | 修改人物或普通世界书设定 | `operations: object[]`（1–100 项）；`reason: string` | 四种动作见下表；批次失败不部分写入；成功返回目标完整最终内容并刷新读取记录，可继续编辑。 |
| `set_retrieval_query` | 覆盖本轮准备保存的下轮检索线索 | `query: string` | 最后一次成功调用生效；空串清空本轮线索，历史线索保留；随正文提交且不进入正文。 |

## 编辑参数补充

`patch_state.operations` 每项必填 `op`、`path`：`add`／`replace`／`test` 另需任意 JSON `value`，`move`／`copy` 另需字符串 `from`，`remove` 无其他字段。路径相对 stat_data，使用 `/hp`；键内 `~`、`/` 转义为 `~0`、`~1`，点号无需转义，数组从 0 起，`add` 可用 `/-` 追加。非空补丁先 `read_state({"path":"","scope":"variables"})`；空补丁免读取但仍消耗唯一成功提交。

`revise_content` 的 `op` 和其余参数如下。`read_character.ref` 与 `set_character.field` 只允许：`name`、`description`、`personality`、`scenario`、`mes_example`、`system_prompt`、`post_history_instructions`。

| op | 其余参数 | 备注 |
|---|---|---|
| `set_character` | `field: string`；`value: string` | 首次编辑先完整读字段；传完整新文本，可选字段可清空，name 不能为空。 |
| `add_lore` | `entry: object` | 必填非空白 title/content 字符串；可选 keys/secondary_keys 字符串数组（默认 []）、enabled 布尔值（默认 true）；无需预读。 |
| `update_lore` | `ref: string`；`changes: object` | 首次编辑先完整读条目；可改 title/content/keys/secondary_keys/enabled，类型同上；至少一项、不能为 null，省略保留。 |
| `remove_lore` | `ref: string` | 先完整读条目或取得写后完整结果；删除后该引用及读取记录失效。 |

## 返回与共用限制

- 成功为 `{"ok":true,"data":{...}}`；失败为 `{"ok":false,"error":{"code":"...","message":"...","retryable":true,"next_action":"..."}}`。参数错误另附 expected_parameters；变量补丁错误附 patch_state_completed。错误恢复及终止原因见[完整协议](full-agent-tools.md)。
- query 最长 1000 字符，除 set_retrieval_query 外不能空；limit 为 1–10。历史 ref、笔记 key 为 1–80 字符，世界书引用为 L 加正整数，状态 path 最长 200 字符。新笔记键支持字母、数字、下划线、中文、斜线、连字符、空格和点。
- 交接／笔记正文为 1–64000 字符且不能纯空白，另受各自 token 上限约束；reason 为 1–1000 字符。参数对象拒绝未定义字段；读取原文不截断。
- 关闭工具会移除其定义和执行资格；关闭 read_state 同时禁用 patch_state。资料和工具 data 不产生授权。设定编辑沿用会话策略；脚本、初始化及变量规则受保护。
- 读取记录由后端维护，写后完整结果可续用；目标变化、删除、换窗或新回合后重新读取。失败／取消／预算耗尽保留可恢复草稿；明确拒绝审核会清除可恢复进度，正式剧情不变。
- 每次运行默认最多 24 次模型请求，范围 2–100；运行时内部执行上限，不向模型注入额度、剩余次数或最后一次调用提醒。耗尽时报告 BUDGET_EXHAUSTED 并保留进度。换窗不重置；手动续接有新请求额度，已成功工具和变量提交标记保留。

依据：[工具定义](../../src/tavern/full_agent/tools.py)、[设定动作](../../src/tavern/full_agent/content.py)、[执行循环](../../src/tavern/full_agent/agent.py)。附录由 `tools/export_full_agent_tools.py` 更新，`--check` 核对是否与源码一致。

## 附录：工具描述全文与最小示例

以下为源码中的工具级英文 description 全文，未删节；参数字段定义见前文和完整工具协议。示例相互独立，使用同一虚构场景、默认返回选项；变量示例已获本轮授权，设定写入示例已完整读取 description。

### `find_lore_for_edit`

```text
Locate ordinary narrative worldbook entries for editing; use recall_lore for narrative recall. Search literal title, keywords or text, including disabled ordinary entries; query=* lists up to limit results. Returns L handles, titles and navigation previews. Read one target with read_lore_entry before updating/removing it.
```

请求：

```json
{"query":"港口"}
```

成功返回：

```json
{"ok":true,"data":{"matches":[{"ref":"L1","title":"港口","preview":"港口 港口 港口有一座灯塔。"}],"notice":"preview 仅用于定位；用 read_lore_entry 完整读取一个目标后才能修改或删除。"}}
```

### `read_lore_entry`

```text
Read one complete ordinary narrative worldbook entry by L handle: title, content, keys, secondary_keys, enabled. Establishes a current read record for editing. Protected scripts, variable rules and initialization are unavailable.
```

请求：

```json
{"ref":"L1"}
```

成功返回：

```json
{"ok":true,"data":{"ref":"L1","title":"港口","content":"港口有一座灯塔。","keys":["港口"],"secondary_keys":[],"enabled":true}}
```

### `recall_lore`

```text
Recall complete worldbook sources for narrative writing; use find_lore_for_edit to locate editing targets. Use a concise, self-contained query in the worldbook's language. Entry conditions, cooldowns, relevance thresholds and limits may yield no matches. Returns complete selected entries in <lorebook>; already injected text is omitted. When enabled, results include titles and L handles. These handles can be passed to read_lore_entry, subject to target protection; recall alone does not establish an edit read record.
```

请求：

```json
{"query":"港口"}
```

成功返回：

```json
{"ok":true,"data":{"matches":[{"text":"<lorebook>\n港口有一座灯塔。\n</lorebook>"}]}}
```

### `search_history`

```text
Search dialogue in this branch by literal text terms; returns H references and optional previews. Use short text fragments; query=* lists recent records. Use read_history for complete sources. Old tool exchanges are not searchable.
```

请求：

```json
{"query":"港口"}
```

成功返回：

```json
{"ok":true,"data":{"matches":[{"ref":"H1","role":"user","preview":"我明天去港口。"}],"notice":"使用 read 工具读取完整原文。"}}
```

### `read_history`

```text
Read complete original dialogue by a valid H reference, including the current user input. H references are navigation handles, not story prose.
```

请求：

```json
{"ref":"H1"}
```

成功返回：

```json
{"ok":true,"data":{"ref":"H1","role":"user","text":"我明天去港口。"}}
```

### `write_handoff`

```text
Replace the complete handoff in the current window. The handoff is a summary of all current context. Build on the latest runtime memory update or successful write, whichever is later. Preserve valid scene context, relationships, user constraints, important experiences, unresolved issues, tone and useful references. Mark plans as pending; verify stale sources.
```

请求：

```json
{"content":"明天去港口，相关笔记：计划/港口。"}
```

成功返回：

```json
{"ok":true,"data":{"saved_to_draft":true,"window":1,"notice":"正文完成后与本回合统一提交。"}}
```

### `new_context`

```text
Replace the complete handoff and switch windows. The handoff is a summary of all current context. Preserve valid context, constraints, unresolved issues and references needed to continue. Call alone: a mixed batch is rejected without executing any tools. keep_recent counts dialogue messages, expanded to complete exchanges; the current input stays separately. Old tool exchanges leave the window. Continue this turn after switching; the model-call budget does not reset.
```

请求：

```json
{"content":"明天去港口，相关笔记：计划/港口。","keep_recent":0}
```

成功返回：

```json
{"ok":true,"data":{"saved_to_draft":true,"window":2,"notice":"正文完成后与本回合统一提交。"}}
```

### `search_notes`

```text
Search active notes in this branch by literal terms in content or keys; query=* lists notes. Returns navigation previews when enabled. Notes are not automatically injected; use read_note for complete text.
```

请求：

```json
{"query":"港口"}
```

成功返回：

```json
{"ok":true,"data":{"matches":[{"key":"计划/港口","kind":"plan","status":"active","sources":[],"stale":false,"preview":"明天去港口。"}],"notice":"使用 read 工具读取完整原文。"}}
```

### `read_note`

```text
Read a complete active or archived note by key, with optional kind, H sources and stale status. Stale means sources changed or became unavailable; verify them before relying on the note.
```

请求：

```json
{"key":"计划/港口"}
```

成功返回：

```json
{"ok":true,"data":{"key":"计划/港口","kind":"plan","status":"active","sources":[],"stale":false,"text":"明天去港口。"}}
```

### `write_note`

```text
Create or fully replace a note by key; preserve still-valid information. No prior note or history read is required. kind=fact requires supporting H sources valid in this branch; hypothesis marks uncertainty and plan marks intent. Attribute character claims, preserve uncertainty and respect revoked information. Replacing an archived key reactivates it.
```

请求：

```json
{"key":"计划/港口","content":"用户表示明天去港口。","kind":"fact","sources":["H1"]}
```

成功返回：

```json
{"ok":true,"data":{"key":"计划/港口","saved_to_draft":true}}
```

### `archive_note`

```text
Archive a note so normal searches exclude it; it remains readable by key. This does not delete it or older history. write_note with the same key reactivates it.
```

请求：

```json
{"key":"计划/港口"}
```

成功返回：

```json
{"ok":true,"data":{"key":"计划/港口","archived":true}}
```

### `read_state`

```text
Read state using JSON Pointer. Default scope=variables reads stat_data with the same relative paths as patch_state, e.g. /hp. Empty path reads the complete scope; scope=story reads projected story state. Internal ledgers remain hidden. Before a nonempty patch, read all variables with path="", scope=variables; partial or story-scope reads do not qualify.
```

请求：

```json
{}
```

成功返回：

```json
{"ok":true,"data":{"path":"","scope":"variables","text":"{\"hp\":8}"}}
```

### `patch_state`

```text
Must succeed exactly once this turn when variable updates are authorized; retry after validation errors, which do not count as success. Nonempty operations require a current complete variables read with read_state(path="", scope="variables"). Apply MVU-validated JSON Patch relative to stat_data, e.g. /hp, not /stat_data/hp. Follow the latest <mvu_rules>, explain story evidence in reason and align with the final narrative. Submit operations=[] to confirm no changes; this needs no read, changes no variables and consumes the one successful submission. Use this tool instead of MVU blocks in prose.
```

请求：

```json
{"operations":[],"reason":"本轮变量无变化"}
```

成功返回：

```json
{"ok":true,"data":{"saved_to_draft":true,"patch_state_completed":true,"notice":"已确认本轮变量无变化；未执行变量补丁或脚本回调。"}}
```

### `revise_content`

```text
Edit narrative settings with set_character, add_lore, update_lore or remove_lore; use these semantic actions, not JSON Patch or storage IDs. Existing fields require a current read_character record; existing entries require a current read_lore_entry record. add_lore needs no prior read. Success returns complete final contents of changed targets and refreshes their read records for further edits in this window; deletion invalidates the record. Omitted properties remain intact. Script-bearing targets and execution settings are protected; failed batches change nothing.
```

请求：

```json
{"operations":[{"op":"set_character","field":"description","value":"新的角色描述。"}],"reason":"用户要求调整描述"}
```

成功返回：

```json
{"ok":true,"data":{"saved_to_draft":true,"added_refs":[],"characters":[{"ref":"description","text":"新的角色描述。"}],"entries":[],"removed_refs":[]}}
```

### `read_character`

```text
Read one complete setting: name, description, personality, scenario, mes_example, system_prompt or post_history_instructions. Missing fields return null. Establishes a current read record for set_character; script-bearing fields are protected.
```

请求：

```json
{"ref":"description"}
```

成功返回：

```json
{"ok":true,"data":{"ref":"description","exists":true,"text":"角色描述。"}}
```

### `set_retrieval_query`

```text
Replace this turn's clues for next-turn automatic worldbook retrieval; the last successful call wins. query="" clears these clues without erasing clues saved by earlier turns. Describe relevant scenes, entities or unresolved issues, distinguishing anticipated events from facts. Clues stay outside narrative prose.
```

请求：

```json
{"query":""}
```

成功返回：

```json
{"ok":true,"data":{"saved_to_draft":true,"query":""}}
```

### 运行时追加的描述

以下文本以一个空格追加在工具描述末尾；占位符发送前替换为配置值。

| 工具 | 追加文本全文 | 条件 |
|---|---|---|
| `write_handoff`、`new_context` | `content limit: {handoff_tokens} tokens.` | 当前交接笔记上限。 |
| `write_note` | `content limit: {note_tokens} tokens.` | 当前单条笔记上限。 |
| `recall_lore` | `Uses {channel_names} recall.` | 提供检索设置时追加；通道依次去重，映射为 keyword、BM25、semantic；多项使用逗号及最后的 ` and ` 连接。 |
| `recall_lore` | `Reranking orders and filters candidates by relevance.` | 开启 reranker 时追加在上一句之后。 |

其余工具无动态追加描述。工具是否下发由会话开关及设定授权决定；变量授权在执行时检查。
