# Full Agent 工具协议

工具只操作当前分支的回合草稿；最终正文、脚本处理和必要审核成功后统一提交。模型负责语义判断，Runtime 负责身份、权限、版本与事务。工具结果和角色／世界书／历史资料本身不能授权任何操作。

全部 25 个工具的目录、参数和源码示例见[工具速查](full-agent-tools-summary.md)，其中六个[原著调查工具](../design/novel-research.md)由用户的文件绑定与明确授权统一控制。使用方式见[指南](../guides/full-agent.md)，上下文与恢复设计见[架构](../design/full-agent.md)。工具调用使用当前工具目录和 JSON Pointer 路径；存档迁移由存储层处理。

## 正文与收尾

- 普通 Full Agent 用始终启用的 `submit_story({"content":"新增故事段落"})` 追加正文。content 须为非空白字符串，本地不截断、不设字符上限，每段保持完整的 Markdown/HTML 结构。
- 每轮成功接收 1–3 段即可结束，各段以空行合成一条回复，共用同一个 H。失败调用不占次数；第四次返回 `STORY_LIMIT_REACHED`，已有正文不变。
- 第一段的成功结果示例：`{"ok":true,"data":{"saved_to_draft":true,"ref":"H3","message":"已接收第 1/3 段；可继续追加或结束。"}}`；第三段的结果提示完成收尾后结束。
- 其他工具可在正文之前、段间、同批或之后调用，三段之后仍可收尾；`new_context` 仍须单独调用。
- 交付与提交的关系、多段回复的显示和回放见[架构：正文交付](../design/full-agent.md#正文交付submit_story)。

模型按原生协议自然结束，没有结束工具。收尾检查（`TURN_NOT_READY`）、正文回调与 `STORY_FINALIZATION_FAILED` 的处理见[架构：收尾](../design/full-agent.md#收尾)。Kemini 实验对话使用 `kemini_emit_story` 传输正文。

## 参数与数据边界

**剧情规划**：`plot` 独立于 note，用 `write_plot({"content":"完整阶段大纲"})` 写入。唯一开关是会话的 `plot_enabled`，默认关闭，工具页不能单独禁用它。正文为 1–64000 字符且非空白，另受 `plot_tokens` 限制（默认 8192 tokens）。当前完整版本通过 `<tavern_plot>` 提供，无需预读或来源引用；成功只返回 `saved_to_draft:true`。开启且大纲为空时，运行时拒绝收尾，要求先成功建纲；之后不要求每轮写入。

玩家接口 `PUT /api/sessions/{sid}/full-agent/plot` 接收 `expected_revision` 和可选的 `enabled`／`content`（至少一项），空字符串清空大纲。空白故事只能改开关，改大纲须已有剧情节点。关闭时保留内容、暂停使用；修改与运行、待审核和上下文整理互斥。手动编辑后的大纲仍可由模型调整。

**参数定义**：由 [tools.py](../../src/tavern/full_agent/tools.py) 和 [content.py](../../src/tavern/full_agent/content.py) 的类型生成，预览和实际请求共用生成器。顶层和嵌套动作都拒绝未定义字段，JSON 类型严格校验。

**引用**：历史引用是当前分支有效的 H 编号——H52 是当前分支第 52 条消息，开场白为 H1，当前用户输入按将要占据的位置编号，与 Half Agent Tracker、参谋 agent 和剧情界面同一定义。世界书引用为 L 加正整数。两者都只是导航句柄，不写入剧情。read_note、write_note、archive_note 统一使用完整笔记 key。版本、指纹、真实 ID 和短编号映射由 Runtime 保存，模型不生成也不回传。

**取值范围**

- query 为 1–1000 字符，只有 set_retrieval_query 允许空串；limit 为 1–10，默认 5。
- 历史 ref 与笔记 key 为 1–80 字符。read_history 的 ref 可为单条 H 或闭区间 `H52-H58`，区间最多 8 条，两端都须在当前分支。read_state.path 最长 200 字符。
- 笔记正文为 1–64000 字符，不能为空白，另受 note_tokens 约束；handoff 的限额见下文。
- comment 为必填的简短操作备注，1–2000 字符。patch_state 和 revise_content 的旧参数 reason 已改为 comment：新调用传 reason 会报错，提示补 comment 并删除 reason；旧调用记录保留原样。
- 新笔记键允许字母、数字、下划线、中文、斜线、连字符、空格和点。

## 叙事检索与编辑定位

recall_lore 使用自动检索的索引与准入规则，返回完整命中正文；已在当前上下文中的自动注入或主动召回正文会省略，允许零命中。主动召回与自动注入共用在场排除和退出后的冷却状态，工具交流退出的那一轮也不重新召回。编号和标题由返回选项控制，默认附标题、不附编号；返回的 L 引用可交给 read_lore_entry，受保护目标仍会被拒绝。召回或自动注入的完整当前正文可以作为修改依据，修改权限仍由本轮授权决定。

find_lore_for_edit 按标题、关键词和正文做字面定位，包括停用的普通条目；query=* 列出最多 limit 项。read_lore_entry 一次返回完整的 title/content/keys/secondary_keys/enabled，并登记该条目的读取记录。历史与笔记搜索也支持 *，但不搜索旧工具过程。

set_retrieval_query 覆盖本轮准备保存的下一轮自动检索线索：最后一次成功调用生效，query="" 清空本轮线索。线索随最终正文提交，不清除旧回合的线索，也不进入正文。成功结果只返回 saved_to_draft，不回显 query。

## 变量路径与授权更新

read_state 的范围：

- `scope="mvu"`（默认）：根对象为投影后的 stat_data；没有 stat_data 时返回 `{}`。
- `scope="full"`：全部模型可见运行时状态，包含 stat_data。
- `scope="other"`：同一状态中除顶层 stat_data 以外的字段。

path 相对所选范围，使用 JSON Pointer，空串读取整个范围，缺失路径返回 `text:"null"`。所有范围都过滤内部账本和 MVU 元数据。只有完整的 mvu 读取或 full 读取中完整的 stat_data 能作为非空变量补丁的依据；other 只用于读取其他运行时字段。

旧工具调用中的 variables／story 分别按 mvu／full 解析和回放，新工具目录只提供 mvu／full／other。替换父对象时保留隐藏字段；数组下标按模型可见项定位，由程序映射到实际存储位置。

| 目标 | 默认变量作用域的 path |
|---|---|
| 所有变量 | `""` |
| hp | `/hp` |
| items 数组首项的 name | `/items/0/name` |
| 名为 a.b 的键 | `/a.b` |
| 名为 a/b~c 的键 | `/a~1b~0c` |

patch_state.operations 为 0–100 项带类型的 JSON Patch：每项 op/path 必填，add/replace/test 需要 value（可为 null），move/copy 需要 from，remove 无其他字段。path/from 相对 stat_data；数组从 0 起，add 可用末尾 `/-`，空指针指根。既有目标、父节点和根值的限制由补丁与 MVU 校验共同检查。

- 授权回合在收尾前须至少成功一次，之后可基于最新状态继续修正草稿。
- 非空补丁需要上下文中的完整当前变量。以下都有效：完整的 mvu 读取或含 stat_data 的 full 读取、工具参数中的完整根值、能由完整读取加成功补丁准确还原的当前值。局部变量、过期值和无法还原的脚本变化不算。缺少当前版本时先 `read_state(path="",scope="mvu")`。
- `operations=[]` 直接确认无变化，不调用补丁阶段的脚本，同样满足本轮变量任务。
- 失败不算成功。补丁失败时恢复脚本镜像并允许修正重试；恢复失败则终止回合。
- 路径错误时给出纠正建议，不自动改写路径；stat_data 中真实存在的同名嵌套键仍合法。

变量规则以本轮固定的完整 mvu_rules 快照提供，规则本身不扩大权限。patch_state 的定义不随本轮授权变化，以保持请求前缀稳定；关闭 patch_state 后不能更新变量。关闭 read_state 不影响 patch_state：已有完整当前版本或提交空补丁时仍可执行。

## 设定编辑与读取记录

read_character.ref 和 set_character.field 的白名单为 name、description、personality、scenario、mes_example、system_prompt、post_history_instructions；每次读取一个完整字段，缺失返回 null。世界书可独立定位和单条读取；不提供整卡、整本、开场白或扩展的读取。

revise_content 接受 1–100 项受限动作和 comment：

| op | 参数 | 规则 |
|---|---|---|
| set_character | field、完整文本 value | 上下文没有该字段的完整最新内容时先读取；可清空可选字段，name 必须有效。 |
| add_lore | entry | 必填非空白 title/content，可选 keys=[]、secondary_keys=[]、enabled=true；不需预读。 |
| update_lore | ref、非空 changes | 上下文没有该条目的完整最新正文时先读取；可改 title/content/keys/secondary_keys/enabled，不能传 null，省略的保持不变。 |
| remove_lore | ref | 上下文须有该普通条目的完整最新正文，否则先读取。 |

- 整批在副本上校验并应用，失败不改变内容或读取记录。成功时在 characters、entries 中返回每个新增／修改目标的完整最终视图（同一目标只返回一次），删除项列入 removed_refs，新增引用列入 added_refs。
- 修改依据：自动注入、完整工具结果、模型自己在工具参数中写出的完整当前文本都算，同窗口内跨回合可沿用。带标题的自动注入按当前配置展开后的完整块核对，不能靠删去任意首行来认定正文完整。目标改变、删除或原文退出上下文后重新核对，没有当前版本时才须读取。读取工具关闭不妨碍依据充分的修改。
- 只开放普通叙事条目（含停用条目）。脚本、HTML 执行入口、tracker_mvu、传统 InitVar 和执行配置受保护，也不能借新文本引入执行内容。无关字段、开场白和扩展保持原样。身份、别名、数组位置和原配置由程序维护，工具不接受整卡替换或任意 JSON Patch。

本轮 `allow_content_edit` 授予设定写入权限，`automatic`／`review` 决定实际修改的保存方式；角色系统及后置字段沿用同一权限。读取与写入工具分别启停：已有目标的完整当前内容仍在上下文中就可以修改，缺少依据时须先启用相应读取工具并补读。新增普通条目直接按参数和权限校验。

## 记忆与换窗

write_note.kind 为 fact、hypothesis 或 plan。事实须给出当前分支有效的 H 来源（包括当前输入和已交付的正文草稿），最多 50 个，无需预先搜索或读取。程序只校验来源属于当前分支，语义是否支持结论由模型判断。其他类型可省略来源，提供时同样校验。来源存储不可变的消息身份；修改输入后的整轮重试与版本切换各用自己的笔记快照。

write_note 完整替换内容。覆盖已有键或用 archive_note 归档前，其完整当前正文须已在上下文中，否则先 read_note；工具参数中自己写出的完整正文也算，写入成功后不必再读。新 key 无需读取，写入归档键会重新激活。目录和搜索摘要不算完整正文。archive_note 只把笔记从普通搜索中隐藏，不删除笔记、历史或旧分支，read_note 仍能读取。模型结果不携带笔记 revision。

**旧版撤回正文的兼容**：恢复旧版已撤回正文的进度时，search_notes 和 read_note 仍能查到引用该正文的笔记，保留其 H 引用，并返回“正文已撤回、需重新核对”的 notice；read_note 关闭 include_metadata 时也保留此提示。重新交付前，该 H 位置不能用 read_history 读取，也不能作为 write_note 的新来源；重新交付沿用同一 H 位置和身份。

handoff 以最新运行时记忆和最近成功写入中较新的一份为准，失败提案不算当前记忆。它由章节和当前交接组成，结构见[handoff 章节](../design/full-agent.md#handoff-章节)。new_context 的 `handoff` 可省略或为 `null`（默认），表示沿用当前交接，内容、版本和来源指纹都不变；需要更新时才传入，结构与 write_handoff 的参数相同：

- `chapters`：0–10 个按顺序执行的操作，C 编号指本次调用前的编号。
  - `add` 通常只给标题和要点，起点自动接在上一章之后。省略 `through` 时，new_context 覆盖到最后一条退出消息，write_handoff 覆盖到最后一条已提交消息；只有主动按语义分章时才用已知 H 填写 `through`。一次调用中省略终点的 add 应放在其他 add 之后。
  - `revise` 改写一章的标题和要点，范围不变；`merge` 把 2–10 个相邻章节合并为一章，范围取并集。
  - title 为 1–80 字符，每章 1–20 条要点。
- `current`：1–30 条要点，整体替换当前交接。
- 要点：`text` 1–2000 字符；`refs` 可省略（默认 `[]`），最多 50 个 H 引用或闭区间；`kind` 为 fact（默认）或 inference。章节引用只能是已提交原文，且须落在本章范围内；`current` 还可引用本轮最新用户输入，或以它为终点的闭区间，但不能引用本轮 AI 正文草稿。引用只用于精确回查，不要为文风、角色资料或指令拼凑 H，这些直接写进交接即可。assistant 正文不带位置标记，精确的 H 从历史工具取得，不需要推算。
- 限额：当前交接 handoff_tokens（默认 8192），单章 chapter_tokens（默认 2048），全部章节 chapters_total_tokens（默认 16384），均按渲染后的文本计数。章节总量超限返回 CONTENT_LIMIT，提示合并最旧的相邻章节。
- `keep_recent`：0–12 条剧情消息，默认 2。保留边界向前扩展到完整交流，当前输入另外保留。只要求章节覆盖最终保留起点之前的原文；保留的消息和当前输入不要求提前归档，全部历史保留时允许沿用空交接。实际退出范围未被覆盖时返回 HANDOFF_COVERAGE，模型可补齐交接，或增大 keep_recent 保留尚未归档的交流。

成功结果返回 `chapters`：当前每章的 C 编号、标题和范围，供同一窗口内后续 revise／merge 使用。旧工具交换整体退出，必要结论应写入记忆。

例：H1 为开场，H2–H7 为三组用户／AI 交流。选择 `keep_recent: 1` 时，最后一条 AI 回复会连同前一条用户消息一起保留，即 H6–H7，章节只需覆盖 H1–H5。现有章节已覆盖时直接调用 `{"keep_recent":1}`，无需重写 handoff。

用户界面只读展示 handoff；手动换窗接口见[架构：handoff 章节](../design/full-agent.md#handoff-章节)。

**批次规则**：new_context 必须单独调用，任何包含它的多工具批次都在执行前整体拒绝（普通状态也是如此）；强制换窗时其他工具和正文也被拒绝。普通工具批次按顺序逐项执行，单项失败不回滚此前成功的项；revise_content 内部的动作批次仍是原子的。

全局设置 `full_agent.tools.parallel_tool_calls` 默认 true，单次响应最多 32 个工具调用；超过时整次响应按 TOOL_PROTOCOL_INVALID 停止，其中工具都不执行，运行时限与请求额度照常生效。续接时移除失败的调用及结果，保留同响应的正文和成功调用，成功工具不重跑。设置关闭时，多业务工具批次中未执行的工具全部返回 PARALLEL_TOOL_CALLS_DISABLED，不产生新的草稿写入，模型应逐次调用。这一限制由运行时统一校验，不依赖服务商是否支持或遵守原生参数；它也不限制 patch_state、revise_content 内合法的多项 operations。

## 返回格式与错误恢复

成功结果统一为：

```json
{"ok":true,"data":{"saved_to_draft":true}}
```

失败结果统一为：

```json
{"ok":false,"error":{"code":"READ_REQUIRED","message":"非空补丁前须完整读取最新变量","retryable":true,"next_action":"调用 read_state，使用默认作用域及空 path。","patch_state_completed":false}}
```

- 参数错误直接指出实际字段位置和修正要求；集合超限时同时报告实际项数。
- 只有需要了解对象或操作结构才能修正时，才在顶层返回 expected_parameters：以出错字段路径为键，附该结构的自包含 schema，不附无关字段或整个工具定义。
- 业务校验直接说明规则，不回显输入或校验器内部信息。
- patch_state 的错误在 error 内另带 patch_state_completed，表明失败是否发生在本轮已有一次成功提交之后。
- 错误与结果都先做模型数据投影，不暴露真实身份和指纹；错误分类来自显式异常，不解析报错文案。

以下情况终止回合，记为诊断事件，不作为可重试的工具结果返回：非法 JSON、非法工具序列或单次超过 32 个调用（TOOL_PROTOCOL_INVALID），正文回调或最终校验失败（STORY_FINALIZATION_FAILED），调用额度耗尽（BUDGET_EXHAUSTED），总时限耗尽（TIME_BUDGET_EXHAUSTED），提供商异常（PROVIDER_ERROR），不完整回复（PROVIDER_INCOMPLETE），脚本恢复失败（SCRIPT_RESTORE_FAILED）。其他运行异常记为 RUNTIME_FAILURE。

## 请求预算与草稿生命周期

- `max_steps` 默认 24，范围 1–100（含两端），按工具循环的模型请求计数，包括工具续答、本地换窗和正文交付后的续答；云端整理另记用量。普通对话的 `submit_story` 计入单次响应的工具调用数。
- `max_duration_seconds` 默认 900 秒，范围 30–7200 秒（含两端），覆盖整次运行。
- 额度或时限耗尽时返回 `BUDGET_EXHAUSTED`／`TIME_BUDGET_EXHAUSTED` 并保留进度；用户明确继续时取得新额度和时限，保留成功工具、正文和变量任务状态。换窗不重置额度。
- 正文与模型结束状态分开保存；已满足业务要求的自然结束可直接在本地重试收尾。

| 情况 | 正式剧情 | 草稿与恢复 |
|---|---|---|
| 正文、脚本处理及审核完成 | 原子提交 | 正文、笔记、变量、设定一起生效。 |
| 生成失败、预算耗尽、取消或中断 | 不变 | 保留进度；只能续接本会话最新且输入／授权一致的可恢复回合，成功工具不重跑。 |
| 等待审核 | 不变 | 整轮保持待提交。 |
| 明确拒绝审核，包括取消待审核回合 | 不变 | 清除 pending、draft、preview 和可续接进度，保留过程记录；续接接口拒绝恢复。 |
| 输入或授权改变 | 不变 | 以新请求重新开始，不复用旧草稿。 |

回合接口返回 resumable 和 resume_failures。resumable 表示当前回合有可恢复进度，实际提交时仍检查它是否为最新回合、版本是否匹配；resume_failures 是连续续接失败的次数。前端只在用户点击“继续本轮”时续接，且要求变量／设定授权一致；“重新开始本轮”和“重新生成”都发起不带 resume_turn_id 的新请求。

错误码表由 `tools/export_full_agent_tools.py` 生成；描述与示例的核对见[速查附录](full-agent-tools-summary.md#附录工具描述全文与最小示例)。

## 交互图形 `present_ui`

普通 Full Agent 可在工具页显式启用，默认关闭；Kemini 实验对话始终不提供。参数为 `{"spec": {...}}`，采用 json-render 标准的 `root/elements/state` 结构；`Stack`、`Text`、`Button`、`Scene` 是本项目注册的受控组件，不是上游通用的内置组件集。完整参数和数字单位由工具描述公开，后端以 `full_agent/visuals.py` 的类型校验为准。

**规格限制**

- 每轮最多成功接收 3 份完整 Spec，失败不占次数。
- 每份最多 40 个元素、1–3 个 Scene，每个 Scene 1–100 个图元。元素必须组成单根树，不接受孤立、重复或循环引用。
- 画布固定 768 CSS 像素宽，高度必填，为 240–600 CSS 像素；坐标是含边界 0–100 的画布百分比。宽高比由高度决定，宽屏和放大窗口中居中显示、不拉伸。
- 图元包括矩形、椭圆、线段、2–30 点的折线/多边形和文字；设置 `showFor` 的图元只在对应的本地选择状态下显示。
- 绘图数据不携带剧情数据库身份。

**交互**：界面状态只有 `selection` 和 `detail`。按钮顶层的 `on.press` 使用 1–3 个受限动作：`setState` 只能写这两个本地路径，`composeDraft` 只向主输入框追加文字并保留已有草稿。图形不能写世界状态、加载 URL、执行脚本或调用模型；推测的关系须以虚线和文字明确区分。用自然语言要求改图会产生新回复，不更新历史原件。

**顺序**：模型按期望的阅读顺序交替调用 submit_story 和 present_ui。只有成功的调用决定正文段和图形的排列；同一原生响应内也按工具调用列表的顺序处理。

**存储与读取**

- 成功调用的工具参数是唯一的持久来源，结果只确认草稿已接收，不回显 Spec。完整工具交换随回合审核、提交、失败恢复、版本和分支处理。
- 图形通过校验后立即作为未提交预览渲染，刷新和续接时从成功工具记录恢复；本轮提交前，图形不会单独成为已保存产物。历史窗口内保留完整参数，不为节省 tokens 截断图形。
- 界面依据成功工具记录，只为确有图形的可见回复按帧合并请求 `POST /api/sessions/{sid}/full-agent/visuals`。请求为 `message_ids`（1–100 条），只返回当前可见版本中结果明确成功的调用。
- 响应的 `messages[mid]` 含 `specs` 和 `order`：`specs` 是按成功调用顺序排列的完整 Spec；`order` 是 `story` / `visual` 标记序列，两种标记分别依次消费正文段与 Spec。失败调用不占位置，调用身份复用仍按各自的配对结果判断。
- 界面使用消息的最终显示正文，不重读或重放 submit_story 的原始正文。成功正文段数与现存分段不一致时，保留完整显示正文，在末尾展示图形，并明确提示无法恢复原来的穿插位置。

## 错误码（由源码生成）

`retryable=true` 表示处理 `next_action` 指出的原因后可再次调用。

| code | 可修正重试 | 默认 next_action |
|---|---|---|
| `STORY_LIMIT_REACHED` | 否 | 完成必要收尾后结束。 |
| `STORY_ALREADY_RECEIVED` | 否 | 保留已接收的正文，继续必要工具或正常结束回复；不要再次提交正文。 |
| `UNKNOWN_TOOL` | 是 | 使用本轮工具目录中的名称。 |
| `PERMISSION_DENIED` | 否 | 遵守本轮授权；需要时由用户调整工具开关或授权。 |
| `INVALID_ARGUMENTS` | 是 | 按参数定义修正后重试。 |
| `INVALID_REFERENCE` | 是 | 重新搜索或读取当前分支中的有效引用。 |
| `READ_REQUIRED` | 是 | 在当前窗口完整读取最新目标后重试。 |
| `INVALID_PATH` | 是 | 使用 JSON Pointer；变量路径相对于 stat_data，如 /hp，不是 stat_data.hp 或 /stat_data/hp。不要自动删除真实同名键。 |
| `PATCH_VALIDATION_FAILED` | 是 | 核对最新变量与 MVU 规则，修正补丁后重试。 |
| `CONTENT_LIMIT` | 是 | 精简本次写入内容后重试；不能截断读取的原文。 |
| `PROTECTED_TARGET` | 否 | 仅修改工具公开的业务字段；程序内部字段与执行配置不可修改。 |
| `MIXED_CONTEXT_CALLS` | 是 | 本批次未执行任何工具；单独调用 new_context。 |
| `PARALLEL_TOOL_CALLS_DISABLED` | 是 | 每次回复只提出一个工具调用，等待结果后再继续。 |
| `CONTEXT_ROLLOVER_REQUIRED` | 是 | 单独调用 new_context 后继续本轮。 |
| `HANDOFF_COVERAGE` | 是 | 补齐实际退出范围的章节，或增大 keep_recent 保留未归档的完整交流，再单独调用 new_context。 |
