# Full Agent 工具速查

当前共 **25 个工具**，其中六个原著调查工具仅在用户绑定文件并开启参考后提供给模型。下表为中文摘要，文末附源码描述全文及每个工具的最小请求／返回示例。带 `=默认值` 的参数可省略，其余必填。

## 如何工作

模型提出工具调用，运行时检查权限、参数和读取记录后执行，把结果送回模型；写入先进入当前分支的回合草稿，正文、脚本处理和必要审核完成后统一提交。模型负责语义判断，程序负责身份定位、来源校验和一致性：H 引用定位剧情，L 引用定位世界书，真实 ID、版本和指纹留在后端。设计说明见 [Full Agent 架构](../design/full-agent.md)。

## 完整工具列表

| 工具名 | 描述 | 参数 | 额外备注 |
|---|---|---|---|
| `submit_story` | 追加本轮新增故事段落 | `content: string` | 必须非空白且结构完整；始终启用，每轮至多成功三次，只追加不改写；普通文本仅作备注。 |
| `present_ui` | 展示冻结的交互图形 | `spec: object`（json-render 规格） | 新建故事默认关闭；Kemini 实验对话不提供。每轮至多接收三份，失败不计数；与 `submit_story` 按调用顺序穿插显示；不接受 HTML、SVG、脚本或 URL，界面操作只改变本地显示或填写输入框。 |
| `start_novel_research` | 异步委派原著调查者 | `question: string`；`background: string=""`；`auto_destroy: boolean=false` | 调查者看不到主上下文，问题需能独立理解；每分支同时最多三个，每轮最多启动五次，销毁或重生成不退回次数。`auto_destroy=true` 在首份最终报告可靠送达后自动销毁，保留报告和历史；报告后仍需复用上下文追问时保持 `false`；提问、失败和暂停不触发。 |
| `list_novel_research` | 查看未销毁的调查者 | 无 | 返回 R 编号、状态、剩余额度、是否有待送达消息、失败原因，以及剩余名额和本轮剩余启动次数；不返回原文过程。 |
| `message_novel_research` | 发送追问或回答 | `ref: string`；`message: string` | 在调查者下一请求前送达，复用原上下文与剩余额度，已暂停或失败的会重新运行；额度耗尽时拒绝。 |
| `wait_novel_research` | 可选等待调查 | `ref: string`；`timeout_seconds: integer=30` | 允许 1–60 秒，只返回状态，超时不取消；报告和提问在本次工具结果之后单独送达。 |
| `resume_novel_research` | 明确恢复调查 | `ref: string` | 只恢复暂停或失败调查，不刷新额度；额度耗尽时拒绝。 |
| `destroy_novel_research` | 销毁调查者 | `ref: string` | 释放名额，保留历史及已交付报告；尚有待送达的报告或提问时拒绝。 |
| `recall_lore` | 为叙事召回完整世界书正文 | `query: string` | 共用自动检索的索引、准入、在场排除与冷却规则；允许零命中，已在上下文中的注入或召回正文省略。可选 L 引用可用于单条读取，完整当前召回正文可作为修改依据。 |
| `find_lore_for_edit` | 定位可编辑的普通条目 | `query: string`；`limit: integer=5` | 字面匹配标题、关键词和正文，包含停用条目；`*` 列出条目；返回 L 引用、标题与定位摘要。 |
| `read_lore_entry` | 完整读取一个普通条目 | `ref: string`（如 `L1`） | 返回标题、正文、主／辅助关键词及启用状态，登记当前读取记录；保护目标不可读写。 |
| `search_history` | 搜索当前分支剧情原文 | `query: string`；`limit: integer=5` | 字面检索，返回 H 引用及可选摘要；`*` 列最近记录，不搜索旧工具过程。 |
| `read_history` | 完整读取剧情原文 | `ref: string`（如 `H1` 或 `H52-H58`） | 可读取当前用户输入和已接收的正文草稿；区间最多 8 条，按分支顺序逐条返回完整原文，H 与角色随“返回编号和角色”设置附带。 |
| `write_handoff` | 更新交接笔记，不换窗 | `chapters: array=[]`；`current: array` | 章节默认范围由程序记录，可 revise／merge；current 整体替换；要点的 H 引用可省略。 |
| `new_context` | 换窗，可同时更新交接笔记 | `handoff: {chapters, current}\|null=null`；`keep_recent: integer=2` | 省略 handoff 沿用现有交接；仅实际退出的原文须有章节覆盖，保留的最新交流不要求归档；保留 0–12 条最近剧情消息并扩展至完整交流，当前输入另保留；必须单独调用，调用额度不重置。 |
| `search_notes` | 搜索有效长期笔记 | `query: string`；`limit: integer=5` | 按键和正文做字面检索；`*` 列笔记，不含归档项。 |
| `read_note` | 完整读取一条笔记 | `key: string` | 可读有效或归档项，可附类型与 H 来源；引用指向对应版本的不可变原文。 |
| `write_note` | 新增或完整替换笔记 | `key: string`；`content: string`；`kind: string`；`sources: string[]=[]` | kind 为 fact／hypothesis／plan；事实必有有效 H 来源，最多 50 个；已有键须有当前全文上下文依据，否则先 read_note，并在当前版本上积累修订（语义要求，不做程序校验）。新 key 无需读取，覆盖归档键会激活。 |
| `archive_note` | 归档笔记 | `key: string` | 当前全文须已在上下文，否则先 read_note；归档项从普通搜索中退出，仍可按键读取或重新激活。 |
| `write_plot` | 完整替换独立的作者阶段大纲 | `content: string` | 仅在剧情规划开启时可用；初次为空须先建纲，随后在当前大纲上积累修订（语义要求，不做程序校验）。全文自动提供，无需预读、来源或版本参数；不与 note.kind=plan 混用。 |
| `read_state` | 按 JSON Pointer 读取状态 | `path: string=""`；`scope: "mvu"或"full"或"other"="mvu"` | mvu 读 stat_data；full 读全部模型可见状态；other 排除顶层 stat_data。路径相对所选范围，空串读完整范围；所有范围都隐藏内部数据，other 不提供变量补丁依据。 |
| `patch_state` | 提交变量补丁或确认无变化 | `operations: object[]`（0–100 项）；`comment: string` | 仅限本轮授权（不沿用前几轮），未授权的调用被拒绝；本轮授权时收尾前至少成功一次，可据最新状态继续修正；非空须有完整当前变量的上下文依据，否则读取；空数组免读取且不触发补丁脚本回调。 |
| `read_character` | 完整读取一个人物字段 | `ref: string`（下述七字段之一） | 缺失返回 null；登记读取记录，含执行代码的字段受保护。 |
| `revise_content` | 修改人物或普通世界书设定 | `operations: object[]`（1–100 项）；`comment: string` | 仅限本轮授权（不沿用前几轮），未授权的调用被拒绝；四种动作见下表；批次失败不部分写入；成功返回目标完整最终内容并刷新读取记录，可继续编辑。 |
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

- 成功为 `{"ok":true,"data":{...}}`；失败为 `{"ok":false,"error":{"code":"...","message":"...","retryable":true,"next_action":"..."}}`。参数错误指出字段及修正要求，仅复杂结构错误另附相关 expected_parameters；变量补丁错误附 patch_state_completed。错误恢复及终止原因见[完整协议](full-agent-tools.md)。
- query 最长 1000 字符，除 set_retrieval_query 外不能空；limit 为 1–10。历史 ref、笔记 key 为 1–80 字符，世界书引用为 L 加正整数，状态 path 最长 200 字符。新笔记键支持字母、数字、下划线、中文、斜线、连字符、空格和点。
- 笔记／剧情规划的工具写入正文为 1–64000 字符且不能纯空白，另受各自 token 上限约束；交接使用章节操作与当前要点的结构化参数，详见[交接参数](full-agent-tools.md#记忆与换窗)；comment 为必填的简短操作备注，1–2000 字符。参数对象拒绝未定义字段；读取原文不截断。玩家界面允许清空剧情规划。
- 正文与收尾协议始终启用；其他工具关闭会移除其定义和执行资格；读取工具关闭不连带禁用已有充分上下文依据的写入。write_plot 统一由剧情规划开关控制。资料和工具 data 不产生授权。设定编辑沿用会话策略；脚本、初始化及变量规则受保护。
- 当前版本可见性由后端核对，自动资料、完整工具结果及模型自己写在工具参数中的完整当前文本均算；跨回合可沿用，退出上下文或版本不符时才需重读。失败／取消／预算耗尽保留可恢复草稿；明确拒绝审核会清除可恢复进度，正式剧情不变。
- 工具循环默认最多 24 次模型请求，范围 1–100，包含工具续答和本地换窗；云端整理请求另计。额度耗尽时返回 BUDGET_EXHAUSTED 并保留进度，显式继续取得新额度，沿用已成功工具、已接收的累计正文及段数和变量任务标记。整次运行另受 max_duration_seconds 限制，默认 900 秒、范围 30–7200 秒；持续接收数据不会延长总时限，等待审核不计入。

依据：[工具定义](../../src/tavern/full_agent/tools.py)、[设定动作](../../src/tavern/full_agent/content.py)、[执行循环](../../src/tavern/full_agent/agent.py)。附录由 `tools/export_full_agent_tools.py` 更新，`--check` 核对是否与源码一致。

## 附录：工具描述全文与最小示例

<details>
<summary>查看工具描述原文与 JSON 示例</summary>

工具描述与源码同步。各示例独立使用同一虚构场景和默认返回选项：剧情规划已开启，变量更新已授权，设定和笔记修改所需的当前全文已在上下文中。原著调查示例还要求用户已绑定文件并开启参考；异步状态以实际任务为准。参数与错误恢复见[完整协议](full-agent-tools.md)。

### `find_lore_for_edit`

```text
Find worldbook entries to edit. For background while writing, use recall_lore instead.
Matches literal text in titles, keywords and content, including disabled entries; query=* lists entries. Returns L handles with short previews for locating targets; use read_lore_entry to see an entry in full. Script and variable-rule entries are not listed.
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
Read one worldbook entry in full by L handle: title, content, keys, secondary_keys and enabled. Script, variable-rule and initialization entries are protected and cannot be read.
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
Look up worldbook background for the story. To find entries to edit, use find_lore_for_edit.
Write a short, self-contained query in the worldbook's language. Returns complete matching entries in <lorebook>. Entries already in this context are left out, and entry conditions, cooldowns or relevance limits may leave no matches. When titles and L handles are shown, recalled entries can serve as the current text for later edits.
```

请求：

```json
{"query":"港口"}
```

成功返回：

```json
{"ok":true,"data":{"matches":[{"title":"港口","text":"<lorebook>\n港口有一座灯塔。\n</lorebook>"}]}}
```

### `search_history`

```text
Search this branch's dialogue by literal text. Use short fragments; query=* lists recent messages. Returns H references with optional previews; use read_history for the full text. Earlier tool calls are not searchable.
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
Read original dialogue in full by H reference (H12) or closed span (H12-H18), including the current user input. A span returns every message in order.
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
Update the handoff without switching windows. Start from the latest <tavern_memory> or your later successful write, whichever is newer, and re-check points marked as having changed sources.
- chapters are an append-only record of what will still matter later: irreversible events, relationship turns, promises and debts, secrets and who knows them, recurring people, items and places. Skip one-off actions, scenery and dialogue texture; the original text stays readable with read_history.
- add: usually give only a title and points. Leave through empty and the chapter covers all committed history not yet in a chapter; set through only to split chapters deliberately.
- Do not rewrite finished chapters. Use revise only to fix an error; merge the oldest adjacent chapters when nearing the total limit.
- current fully replaces the current handoff: the scene, relationships, constraints, unresolved issues and tone. Do not retell chapter events.
- refs are optional. Cite a known H reference only when precise lookup will help later; never invent one.
- Do not copy whole notes or the plot outline into the handoff.
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
Switch to a fresh context window. Older messages and earlier tool calls leave the window; afterwards you continue the same turn, and the call budget does not reset.
- When to switch: the runtime measures context usage for you, so do not estimate it yourself. Without a context notice, do not switch. A budget reminder means plan the switch, ideally at a natural break; a forced-rollover notice means switch now.
- keep_recent sets how many recent messages stay. It is rounded up to whole user/assistant exchanges, and the current input always stays.
- Every message that leaves must already be covered by a chapter. If it is, call with just keep_recent. Otherwise pass handoff with add operations for the missing part, leaving through empty. If all history stays, an empty handoff is fine.
- Omitting handoff keeps the current one unchanged. When you pass it, chapters keep lasting events, relationship changes, promises, secrets and recurring people, items and places; current holds the scene, relationships, constraints, unresolved issues and tone. Do not copy whole notes or the plot outline.
Call alone: a batch containing new_context is rejected and none of its tools run.
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
Search active notes by literal text in keys or content; query=* lists notes. Returns keys with optional previews. Note contents are not automatically injected; use read_note for the full text.
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
Read a note in full by key, including archived notes. Metadata may include kind and H sources.
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
Create a note or fully replace one by key. Updates are cumulative: start from the note's current text, keep information that is still valid, and record how things happened and changed, not just the current state. Attribute claims to whoever made them, keep uncertainty visible, and treat retracted information as no longer valid.
kind: fact needs H sources from this branch; hypothesis marks uncertainty; plan records a character's own intentions inside the story. Author-level outlines belong in write_plot.
Writing to an archived key reactivates it.
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
Create or fully replace this branch's author-level outline of where the story may go. The current outline is in <tavern_plot>; no read is needed.
Updates are cumulative: revise the current outline instead of starting over, keeping its structure and everything that still holds. The tool protocol describes the suggested sections.
Contents are revisable possibilities, not established facts, things characters know, or actions decided for the user.
```

请求：

```json
{"content":"## 核心\n- 守塔人为何藏起旧信？\n## 各方动向\n- 守塔人：想让旧信永远沉默；若用户追问，会提前出海。\n## 当前阶段\n- 若用户前往港口，守塔人的隐瞒引出冲突。\n## 伏笔\n- 旧信的火漆：远期视选择揭开来历，尚未回收。"}
```

成功返回：

```json
{"ok":true,"data":{"saved_to_draft":true}}
```

### `archive_note`

```text
Archive a note that is no longer relevant. It drops out of normal searches but stays readable by key; nothing is deleted. Writing to the same key reactivates it.
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
Read runtime variables by JSON Pointer.
- scope=mvu (default): the MVU variables (stat_data), with the same paths as patch_state, e.g. /hp.
- scope=full: all visible runtime state, including stat_data.
- scope=other: everything except stat_data.
An empty path reads the whole scope. To refresh all MVU variables before patching, use path="" and scope="mvu".
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
Update MVU variables with JSON Patch. Only for turns whose instructions authorize variable updates; authorization from earlier turns does not carry over, and calls without it are REJECTED. When variable updates are authorized, submit at least one successful patch this turn; validation failures may be corrected and retried and do not count as success. If nothing changed, submit operations=[]; this needs no read and changes nothing.
Paths are relative to stat_data: /hp, not /stat_data/hp. Follow the latest <mvu_rules>, keep values consistent with the final story text, and cite the story evidence in comment. Use this tool instead of writing MVU blocks in the story text.
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
Edit character settings and worldbook entries with semantic actions: set_character, add_lore, update_lore and remove_lore. Use these actions, not JSON Patch or storage IDs.
Only for turns whose instructions authorize content edits; authorization from earlier turns does not carry over, and calls without it are REJECTED.
Omitted properties stay unchanged, and a failed batch changes nothing. Success returns the final contents of each changed target, which count as current for later edits. Script-bearing targets and execution settings are protected.
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
Read one character setting in full: name, description, personality, scenario, mes_example, system_prompt or post_history_instructions. Missing fields return null; script-bearing fields are protected.
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
Set clues for next turn's automatic worldbook lookup: the scenes, people, places or unresolved issues likely to matter, with anticipated events marked as anticipated. The last successful call this turn wins; query="" clears this turn's clues without touching earlier turns. Clues never appear in the story.
```

请求：

```json
{"query":""}
```

成功返回：

```json
{"ok":true,"data":{"saved_to_draft":true}}
```

### `start_novel_research`

```text
Delegate a question about the user's novel to a new read-only investigator. Returns an R reference immediately; the investigator runs in the background and can continue across turns. It cannot see your context, so write a self-contained question and pass only the background it needs. Limits: 3 investigators per branch until destroyed, 5 starts per story turn; destroying or regenerating does not return a start.
```

请求：

```json
{"question":"查证人物早年经历","background":"本局尚未遇见此人"}
```

成功返回：

```json
{"ok":true,"data":{"ref":"R1","status":"running","created":true,"notice":"调查已在后台开始。本轮正文需要结论时，用 wait_novel_research 等待。"}}
```

### `list_novel_research`

```text
List investigators that are not destroyed: status, remaining requests and seconds, whether a report or question is waiting to be delivered, and any failure reason. Also returns free slots and starts left this turn.
```

请求：

```json
{}
```

成功返回：

```json
{"ok":true,"data":{"investigators":[],"slots_remaining":3,"starts_remaining_this_turn":5}}
```

### `message_novel_research`

```text
Send a follow-up question, or an answer to an investigator's question. It continues in its existing context with its remaining quota, restarting if paused or failed; quota is never refilled, and an investigator with no quota left cannot take messages.
```

请求：

```json
{"ref":"R1","message":"请注明原著时期"}
```

成功返回：

```json
{"ok":true,"data":{"ref":"R1","status":"running","notice":"报告、提问和失败通知会在本次工具结果之后作为单独消息出现；已发出的正文不会被改写。"}}
```

### `wait_novel_research`

```text
Wait until an investigator reports, asks a question or stops running. Returns its status only; a report or question appears as a separate message right after this result. Timing out does not cancel the investigator: wait again, or continue without it.
```

请求：

```json
{"ref":"R1","timeout_seconds":30}
```

成功返回：

```json
{"ok":true,"data":{"ref":"R1","status":"completed","notice":"报告、提问和失败通知会在本次工具结果之后作为单独消息出现；已发出的正文不会被改写。"}}
```

### `resume_novel_research`

```text
Restart a paused or failed investigator in its existing context with its remaining quota. Quota is never refilled; an investigator with no quota left cannot be resumed.
```

请求：

```json
{"ref":"R1"}
```

成功返回：

```json
{"ok":true,"data":{"ref":"R1","status":"running","notice":"报告、提问和失败通知会在本次工具结果之后作为单独消息出现；已发出的正文不会被改写。"}}
```

### `destroy_novel_research`

```text
Cancel an investigator and free its slot; delivered reports and history are kept. Use once its report and any needed follow-ups are done; keep it only for a concrete follow-up. Refused while a report or question from it is still waiting to be delivered.
```

请求：

```json
{"ref":"R1"}
```

成功返回：

```json
{"ok":true,"data":{"ref":"R1","status":"destroyed"}}
```

### `present_ui`

```text
Show a frozen interactive graphic when it helps (map, relationship graph, structure diagram); do not repeat panels every turn. Call submit_story and present_ui in the intended reading order, including within one batch: accepted calls determine how prose segments and graphics are interleaved. At most 3 accepted specs per turn; failed calls do not count. To change an older graphic, send a new complete spec in a later turn. No HTML, SVG markup, scripts or URLs.
Spec: {root:'root',elements:{root:{type:'Stack',props:{direction:'vertical'},children:['map','go']},map:{type:'Scene',props:{...},children:[]},go:{type:'Button',props:{label:'Go'},on:{press:{...}},children:[]}},state:{selection:'overview',detail:''}}. Every element has type, props and children; only Stack has children. Keys: a letter, then letters/digits/_/- (max 31). At most 40 elements forming one tree from root, with 1-3 Scenes. state has exactly selection (max 40 chars) and detail (max 4000).
Stack: direction 'vertical'|'horizontal'. Text: text (string or {$state:'/detail'}). Button: label (max 60); on.press sits beside props.
Scene: title (max 120), height (240-600 px), selection {$state:'/selection'}, items (1-100, drawn in order; put labels last). The canvas is 768 px wide: coordinates are 0-100 percent, x/width/rx of 768 and y/height/ry of height.
Items have kind and optional showFor (1-12 selection values; omit to always show): rect x,y,width,height,fill,stroke; ellipse x,y,rx,ry,fill,stroke; line x1,y1,x2,y2,stroke,dashed?; path points [[x,y],...] (2-30, straight segments),closed,fill,stroke,dashed?; label x,y,text (max 200),anchor 'start'|'middle'|'end'. Paint: none|surface|muted|ink|accent|secondary. Mark unconfirmed relations with dashed lines and labels; keep labels apart.
press is one action or a list of 1-3: setState {statePath:'/selection'|'/detail',value}, or composeDraft {text: string or {$state:'/detail'}}, which only appends to the user's input for them to edit and send; never imply it happened. Prose goes in submit_story; do not restate the spec.
```

请求：

```json
{"spec":{"root":"map","elements":{"map":{"type":"Scene","props":{"title":"港口","height":240,"selection":{"$state":"/selection"},"items":[{"kind":"label","x":50,"y":50,"text":"灯塔","anchor":"middle"}]},"children":[]}},"state":{"selection":"overview","detail":""}}}
```

成功返回：

```json
{"ok":true,"data":{"saved_to_draft":true,"message":"图形已接收，随本轮正文统一提交；界面操作仅改变本地显示或填写输入框。"}}
```

### `submit_story`

```text
Append a new story segment to this turn's draft. At most 3 successful submissions per turn; 1 or 2 segments are sufficient when the story is complete. Failed submissions do not use a slot. Segments share one H reference and are joined with blank lines. Never repeat or revise accepted prose. Ordinary assistant text is commentary. Tools may run before, between or after segments, including after the third. Check variables and notes against the entire story, then end normally; delivery does not commit the turn.
```

请求：

```json
{"content":"她在港口的灯塔下等你。"}
```

成功返回：

```json
{"ok":true,"data":{"saved_to_draft":true,"ref":"H2","message":"已接收第 1/3 段；可继续追加或结束。"}}
```

### 运行时追加的描述

以下文本以一个空格追加在工具描述末尾；占位符发送前替换为配置值。

| 工具 | 追加文本全文 | 条件 |
|---|---|---|
| `write_handoff` | `Limits: current {handoff_tokens} tokens, each chapter {chapter_tokens}, all chapters {chapters_total_tokens}.` | 当前交接、单章与章节总量上限。 |
| `new_context` | `Limits: current {handoff_tokens} tokens, each chapter {chapter_tokens}, all chapters {chapters_total_tokens}.` | 同上，适用于换窗时提交的交接。 |
| `write_note` | `Content limit: {note_tokens} tokens.` | 当前单条笔记上限。 |
| `write_plot` | `Content limit: {plot_tokens} tokens.` | 当前剧情规划上限。 |
| `recall_lore` | `Uses {channel_names} recall.` | 提供检索设置时追加；通道依次去重，映射为 keyword、BM25、semantic；多项使用逗号及最后的 ` and ` 连接。 |
| `recall_lore` | `Reranking orders and filters candidates by relevance.` | 开启 reranker 时追加在上一句之后。 |

工具目录由会话开关及相关功能确定，变量与设定授权在执行时检查。动态追加描述见上表。

</details>
