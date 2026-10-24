# Full Agent 工具速查

当前共 **17 个工具**。下表为中文摘要，文末附源码描述全文及每个工具的最小请求／返回示例。带 `=默认值` 的参数可省略，其余必填。

## 如何工作、为何这样设计

Full Agent 由同一个模型生成剧情、查资料和维护状态。后端把工具名称、说明和 JSON 参数结构交给模型；模型提出调用，运行时检查权限、参数及读取记录，执行后把结果送回模型，普通文本输出一次完整正文，并按模型原生协议自然结束。写入先进入当前分支的回合草稿，正文、脚本处理及必要审核完成后统一提交。

设计目的是让模型按需取得完整资料，用交接笔记和长期笔记延续故事，并在授权下维护变量与设定。模型负责语义判断，程序负责身份定位、来源校验和一致性：H 引用定位剧情，L 引用定位世界书，真实 ID、版本和指纹留在后端。换窗降低上下文占用，原始剧情仍可查询；长期笔记按需读取，不自动全文注入。

## 完整工具列表

| 工具名 | 描述 | 参数 | 额外备注 |
|---|---|---|---|
| `recall_lore` | 为叙事召回完整世界书正文 | `query: string` | 共用自动检索的索引、准入、在场排除与冷却规则；允许零命中，已在上下文中的注入或召回正文省略。可选 L 引用可用于单条读取，完整当前召回正文可作为修改依据。 |
| `find_lore_for_edit` | 定位可编辑的普通条目 | `query: string`；`limit: integer=5` | 字面匹配标题、关键词和正文，包含停用条目；`*` 列出条目；返回 L 引用、标题与定位摘要。 |
| `read_lore_entry` | 完整读取一个普通条目 | `ref: string`（如 `L1`） | 返回标题、正文、主／辅助关键词及启用状态，登记当前读取记录；保护目标不可读写。 |
| `search_history` | 搜索当前分支剧情原文 | `query: string`；`limit: integer=5` | 字面检索，返回 H 引用及可选摘要；`*` 列最近记录，不搜索旧工具过程。 |
| `read_history` | 完整读取剧情原文 | `ref: string`（如 `H1` 或 `H52-H58`） | 可读取当前用户输入和已接收的正文草稿；区间最多 8 条，按分支顺序逐条返回完整原文，H 与角色随“返回编号和角色”设置附带。 |
| `write_handoff` | 更新交接笔记，不换窗 | `chapters: array=[]`；`current: array` | 章节默认范围由程序记录，可 revise／merge；current 整体替换；要点的 H 引用可省略。 |
| `new_context` | 换窗，可同时更新交接笔记 | `handoff: {chapters, current}\|null=null`；`keep_recent: integer=2` | 省略 handoff 沿用现有交接；仅实际退出的原文须有章节覆盖，保留的最新交流不要求归档；保留 0–12 条最近剧情消息并扩展至完整交流，当前输入另保留；必须单独调用，调用额度不重置。 |
| `search_notes` | 搜索有效长期笔记 | `query: string`；`limit: integer=5` | 按键和正文做字面检索；`*` 列笔记，不含归档项。 |
| `read_note` | 完整读取一条笔记 | `key: string` | 可读有效或归档项，可附类型与 H 来源；引用指向对应版本的不可变原文。 |
| `write_note` | 新增或完整替换笔记 | `key: string`；`content: string`；`kind: string`；`sources: string[]=[]` | kind 为 fact／hypothesis／plan；事实必有有效 H 来源，最多 20 个；已有键须有当前全文上下文依据，否则先 read_note。新 key 无需读取，覆盖归档键会激活。 |
| `archive_note` | 归档笔记 | `key: string` | 当前全文须已在上下文，否则先 read_note；归档项从普通搜索中退出，仍可按键读取或重新激活。 |
| `write_plot` | 完整替换独立的作者阶段大纲 | `content: string` | 仅在剧情规划开启时可用；初次为空须先建纲，随后按需更新。全文自动提供，无需预读、来源或版本参数；不与 note.kind=plan 混用。 |
| `read_state` | 按 JSON Pointer 读取状态 | `path: string=""`；`scope: "mvu"或"full"或"other"="mvu"` | mvu 读 stat_data；full 读全部模型可见状态；other 排除顶层 stat_data。路径相对所选范围，空串读完整范围；所有范围都隐藏内部数据，other 不提供变量补丁依据。 |
| `patch_state` | 提交变量补丁或确认无变化 | `operations: object[]`（0–100 项）；`comment: string` | 本轮授权时收尾前至少成功一次，可据最新状态继续修正；非空须有完整当前变量的上下文依据，否则读取；空数组免读取且不触发补丁脚本回调。 |
| `read_character` | 完整读取一个人物字段 | `ref: string`（下述七字段之一） | 缺失返回 null；登记读取记录，含执行代码的字段受保护。 |
| `revise_content` | 修改人物或普通世界书设定 | `operations: object[]`（1–100 项）；`comment: string` | 四种动作见下表；批次失败不部分写入；成功返回目标完整最终内容并刷新读取记录，可继续编辑。 |
| `set_retrieval_query` | 覆盖本轮准备保存的下轮检索线索 | `query: string` | 最后一次成功调用生效；空串清空本轮线索，历史线索保留；随正文提交且不进入正文。 |

## 编辑参数补充

`patch_state.operations` 每项必填 `op`、`path`：`add`／`replace`／`test` 另需任意 JSON `value`，`move`／`copy` 另需字符串 `from`，`remove` 无其他字段。路径相对 stat_data，使用 `/hp`；键内 `~`、`/` 转义为 `~0`、`~1`，点号无需转义，数组从 0 起，`add` 可用 `/-` 追加。非空补丁需要完整当前变量的上下文依据，没有时先 `read_state({"path":"","scope":"mvu"})`；空补丁免读取并满足本轮变量任务要求。

`revise_content` 的 `op` 和其余参数如下。`read_character.ref` 与 `set_character.field` 只允许：`name`、`description`、`personality`、`scenario`、`mes_example`、`system_prompt`、`post_history_instructions`。

| op | 其余参数 | 备注 |
|---|---|---|
| `set_character` | `field: string`；`value: string` | 缺少当前完整字段时先读取；传完整新文本，可选字段可清空，name 不能为空。 |
| `add_lore` | `entry: object` | 必填非空白 title/content 字符串；可选 keys/secondary_keys 字符串数组（默认 []）、enabled 布尔值（默认 true）；无需预读。 |
| `update_lore` | `ref: string`；`changes: object` | 缺少当前完整条目正文时先读取；可改 title/content/keys/secondary_keys/enabled，类型同上；至少一项、不能为 null，省略保留。 |
| `remove_lore` | `ref: string` | 当前完整条目正文须在上下文，否则读取；删除后该引用及读取记录失效。 |

## 返回与共用限制

- 成功为 `{"ok":true,"data":{...}}`；失败为 `{"ok":false,"error":{"code":"...","message":"...","retryable":true,"next_action":"..."}}`。参数错误另附 expected_parameters；变量补丁错误附 patch_state_completed。错误恢复及终止原因见[完整协议](full-agent-tools.md)。
- query 最长 1000 字符，除 set_retrieval_query 外不能空；limit 为 1–10。历史 ref、笔记 key 为 1–80 字符，世界书引用为 L 加正整数，状态 path 最长 200 字符。新笔记键支持字母、数字、下划线、中文、斜线、连字符、空格和点。
- 交接／笔记／剧情规划的工具写入正文为 1–64000 字符且不能纯空白，另受各自 token 上限约束；comment 为必填的简短操作备注，1–1000 字符。参数对象拒绝未定义字段；读取原文不截断。玩家界面允许清空剧情规划。
- 正文与收尾协议始终启用；其他工具关闭会移除其定义和执行资格；读取工具关闭不连带禁用已有充分上下文依据的写入。write_plot 统一由剧情规划开关控制。资料和工具 data 不产生授权。设定编辑沿用会话策略；脚本、初始化及变量规则受保护。
- 当前版本可见性由后端核对，自动资料、完整工具结果及模型自己写在工具参数中的完整当前文本均算；跨回合可沿用，退出上下文或版本不符时才需重读。失败／取消／预算耗尽保留可恢复草稿；明确拒绝审核会清除可恢复进度，正式剧情不变。
- 工具循环默认最多 24 次模型请求，范围 1–100，包含工具续答和本地换窗；云端整理请求另计。额度耗尽时返回 BUDGET_EXHAUSTED 并保留进度，显式继续取得新额度，沿用已成功工具、唯一正文和变量任务标记。整次运行另受 max_duration_seconds 限制，默认 900 秒、范围 30–7200 秒；持续接收数据不会延长总时限，等待审核不计入。

依据：[工具定义](../../src/tavern/full_agent/tools.py)、[设定动作](../../src/tavern/full_agent/content.py)、[执行循环](../../src/tavern/full_agent/agent.py)。附录由 `tools/export_full_agent_tools.py` 更新，`--check` 核对是否与源码一致。

## 附录：工具描述全文与最小示例

<details>
<summary>查看工具描述原文与 JSON 示例</summary>

工具描述与源码同步。各示例独立使用同一虚构场景和默认返回选项：剧情规划已开启，变量更新已授权，设定和笔记修改所需的当前全文已在上下文中。参数与错误恢复见[完整协议](full-agent-tools.md)。

### `find_lore_for_edit`

```text
Locate ordinary narrative worldbook entries for editing; use recall_lore for narrative recall. Search literal title, keywords or text, including disabled ordinary entries; query=* lists up to limit results. Returns L handles, titles and navigation previews. Previews help locate targets; use read_lore_entry when you need to inspect or refresh a target's complete contents.
```

请求：

```json
{"query":"港口"}
```

成功返回：

```json
{"ok":true,"data":{"matches":[{"ref":"L1","title":"港口","preview":"港口 港口 港口有一座灯塔。"}],"notice":"preview 仅用于定位；需要了解或核对目标完整内容时，使用 read_lore_entry。"}}
```

### `read_lore_entry`

```text
Inspect or refresh one complete ordinary narrative worldbook entry by L handle: title, content, keys, secondary_keys, enabled. Protected scripts, variable rules and initialization are unavailable.
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
Recall complete worldbook sources for narrative writing; use find_lore_for_edit to locate editing targets. Use a concise, self-contained query in the worldbook's language. Entry conditions, cooldowns, relevance thresholds and limits may yield no matches. Returns complete selected entries in <lorebook>; text already injected or recalled in this context is omitted. Recalled entries share automatic injection's presence and cooldown rules. When enabled, results include titles and L handles. Reuse recalled content for later edits when you know it is still current; target protection still applies.
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
Read complete original dialogue by a valid H reference or closed span, including the current user input; a span returns its messages complete and in branch order. H references are navigation handles, not story prose.
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
Update the handoff in the current window without switching: optional chapter operations plus the complete current handoff. Build on the latest <tavern_memory> or successful write, whichever is later; verify points marked as changed sources. Chapters are an append-only ledger of what still matters later (irreversible events, relationship turns, promises and debts, secrets and who knows them, recurring people, items and places). Normally omit through: Runtime appends a chapter over the unarchived committed history. Choose a known endpoint only for intentional chapter splits. Drop one-off actions, scenery and dialogue texture; chapter ranges remain readable with read_history. Point refs are optional: cite known history only when useful for precise follow-up, never invent a citation for instructions or character material. Do not rewrite written chapters; revise only to correct one, merge the oldest adjacent chapters when the total limit is near. current fully replaces the current handoff: scene, relationships, constraints, unresolved issues and tone, without retelling chapter events. Do not copy complete notes or the future plot outline.
```

请求：

```json
{"current":[{"text":"明天去港口，相关笔记：计划/港口。"}]}
```

成功返回：

```json
{"ok":true,"data":{"saved_to_draft":true,"window":1,"chapters":[],"notice":"正文完成后与本回合统一提交。"}}
```

### `new_context`

```text
Switch windows, optionally updating the handoff. Omit handoff or pass null to reuse the latest saved or successfully written handoff unchanged. Choose keep_recent first: chapter coverage is required only for messages actually leaving after expanding the retained tail to complete exchanges. Retained messages and the current input need no new chapter; when all history stays, an empty handoff is allowed. If updating, add any missing chapters and give the complete current handoff. Normally omit through on add: Runtime assigns the remaining exiting range. Choose a known endpoint only for intentional chapter splits. Keep only lasting events, relationship changes, promises, secrets and recurring people, items and places; chapter ranges remain readable with read_history. Point refs are optional, including in current; use only known references when precise follow-up helps. Do not rewrite written chapters; revise only to correct one, merge the oldest adjacent chapters when the total limit is near. current fully replaces the current handoff: scene, relationships, constraints, unresolved issues and tone, without retelling chapter events. Do not copy complete notes or the future plot outline. Call alone: a mixed batch is rejected without executing any tools. keep_recent counts dialogue messages, expanded to complete exchanges; the current input stays separately. Old tool exchanges leave the window. Continue this turn after switching; the model-call budget does not reset.
```

请求：

```json
{}
```

成功返回：

```json
{"ok":true,"data":{"saved_to_draft":true,"window":2,"chapters":[],"notice":"正文完成后与本回合统一提交。"}}
```

### `search_notes`

```text
Search active notes in this branch by literal terms in content or keys; query=* lists notes. Returns navigation previews when enabled. Note contents are not automatically injected; use read_note for complete text.
```

请求：

```json
{"query":"港口"}
```

成功返回：

```json
{"ok":true,"data":{"matches":[{"key":"计划/港口","kind":"plan","status":"active","sources":[],"preview":"明天去港口。"}],"notice":"使用 read 工具读取完整原文。"}}
```

### `read_note`

```text
Read a complete active or archived note by key, with optional kind and H sources.
```

请求：

```json
{"key":"计划/港口"}
```

成功返回：

```json
{"ok":true,"data":{"key":"计划/港口","kind":"plan","status":"active","sources":[],"text":"明天去港口。"}}
```

### `write_note`

```text
Create or fully replace a note by key; preserve still-valid information. Build on the current contents you already know, including text you wrote in earlier tool calls. Use read_note when you do not know the note's current contents or are unsure whether your version is still current. New keys need no read. kind=fact requires supporting H sources valid in this branch; hypothesis marks uncertainty and plan marks characters' in-story intentions, not an author-level future outline. Preserve valid experiences and changes, not just a current-state snapshot. Attribute character claims, preserve uncertainty and respect revoked information. Replacing an archived key reactivates it.
```

请求：

```json
{"key":"计划/港口","content":"用户表示明天去港口。","kind":"fact","sources":["H1"]}
```

成功返回：

```json
{"ok":true,"data":{"key":"计划/港口","saved_to_draft":true}}
```

### `write_plot`

```text
Create or fully replace this branch's rolling author-level stage outline. Available only when plot planning is enabled. The current complete outline is supplied automatically; no prior read, source references or version argument is required. When empty, save an initial outline before ending the turn. Otherwise revise when stages finish, turning points occur, the player diverges or premises change; no per-turn rewrite is required. Keep near stages concrete and distant stages broad, with possible events, turns, foreshadowing and conditions. These are revisable possibilities, not established facts, character knowledge or predetermined player actions. Player edits remain an editable draft. Success saves to the turn draft; failure preserves the old outline.
```

请求：

```json
{"content":"近期：若玩家前往港口，让守塔人的隐瞒引出冲突；远期：视选择揭开旧信来历。"}
```

成功返回：

```json
{"ok":true,"data":{"saved_to_draft":true}}
```

### `archive_note`

```text
Archive a note so normal searches exclude it; it remains readable by key. Use what you already know about the note to decide whether to archive it. Use read_note if you do not know its current contents or need to check them. This does not delete the note or older history. write_note with the same key reactivates it.
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
Inspect or refresh runtime state using JSON Pointer relative to the selected scope. Default scope=mvu reads stat_data with the same relative paths as patch_state, e.g. /hp. scope=full reads all model-visible runtime state, including stat_data; scope=other reads that state excluding the top-level stat_data field. Empty path reads the complete selected scope. Internal ledgers remain hidden in every scope. To refresh all MVU variables for patching, use path="", scope="mvu".
```

请求：

```json
{}
```

成功返回：

```json
{"ok":true,"data":{"path":"","scope":"mvu","text":"{\"hp\":8}"}}
```

### `patch_state`

```text
When variable updates are authorized, submit at least one successful patch this turn; validation failures may be corrected and retried and do not count as success. Work from the current MVU values you already know, including your earlier tool calls and their successful results. If you do not know the current values or are unsure whether they are still current, use read_state(path="", scope="mvu") to refresh them. Apply MVU-validated JSON Patch relative to stat_data, e.g. /hp, not /stat_data/hp. Follow the latest <mvu_rules> and align with the final narrative. Include relevant story evidence in a brief user-facing comment. Submit operations=[] to confirm no changes; this needs no read and changes no variables. Use this tool instead of MVU blocks in prose.
```

请求：

```json
{"operations":[],"comment":"本轮变量无变化"}
```

成功返回：

```json
{"ok":true,"data":{"saved_to_draft":true,"patch_state_completed":true,"notice":"已确认本轮变量无变化；未执行变量补丁或脚本回调。"}}
```

### `revise_content`

```text
Edit narrative settings with set_character, add_lore, update_lore or remove_lore; use these semantic actions, not JSON Patch or storage IDs. Work from the current contents you already know, including supplied settings, tool results and text you wrote in earlier tool calls. Use read_character or read_lore_entry when you do not know the target's current contents or are unsure whether your version is still current. Adding a new entry needs no read. Success returns complete final contents of changed targets, which you can reuse for later edits. Omitted properties remain intact. Script-bearing targets and execution settings are protected; failed batches change nothing.
```

请求：

```json
{"operations":[{"op":"set_character","field":"description","value":"新的角色描述。"}],"comment":"用户要求调整描述"}
```

成功返回：

```json
{"ok":true,"data":{"saved_to_draft":true,"added_refs":[],"characters":[{"ref":"description","text":"新的角色描述。"}],"entries":[],"removed_refs":[]}}
```

### `read_character`

```text
Inspect or refresh one complete setting: name, description, personality, scenario, mes_example, system_prompt or post_history_instructions. Missing fields return null. Script-bearing fields are protected.
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
{"ok":true,"data":{"saved_to_draft":true}}
```

### 运行时追加的描述

以下文本以一个空格追加在工具描述末尾；占位符发送前替换为配置值。

| 工具 | 追加文本全文 | 条件 |
|---|---|---|
| `write_handoff` | `Limits: current {handoff_tokens} tokens, each chapter {chapter_tokens}, all chapters {chapters_total_tokens}.` | 当前交接、单章与章节总量上限。 |
| `new_context` | `Limits: current {handoff_tokens} tokens, each chapter {chapter_tokens}, all chapters {chapters_total_tokens}.` | 同上，适用于换窗时提交的交接。 |
| `write_note` | `content limit: {note_tokens} tokens.` | 当前单条笔记上限。 |
| `write_plot` | `content limit: {plot_tokens} tokens.` | 当前剧情规划上限。 |
| `recall_lore` | `Uses {channel_names} recall.` | 提供检索设置时追加；通道依次去重，映射为 keyword、BM25、semantic；多项使用逗号及最后的 ` and ` 连接。 |
| `recall_lore` | `Reranking orders and filters candidates by relevance.` | 开启 reranker 时追加在上一句之后。 |

工具目录由会话开关及设定授权确定，变量授权在执行时检查。动态追加描述见上表。

</details>
