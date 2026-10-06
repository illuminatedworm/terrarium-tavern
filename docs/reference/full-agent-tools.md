# Full Agent 工具协议

工具运行在本回合草稿上。成功的工具结果表示草稿已接受，正文、脚本处理和审核完成后才提交到剧情。工具开关、操作步骤见[使用指南](../guides/full-agent.md)，窗口与恢复机制见[架构](../design/full-agent.md)。

## 工具目录

参数由 [full_agent/tools.py](../../src/tavern/full_agent/tools.py) 中的类型定义生成，预览和实际请求使用同一生成器。参数对象拒绝未定义字段。

| 工具 | 参数 | 结果与约束 |
|---|---|---|
| `search_read_lore` | `query` | 使用世界书索引和准入规则，直接返回完整正文；已在上下文的条目省略正文 |
| `search_lore_entries` | `query`，`limit=5` | 按标题、关键词和正文定位可编辑普通条目，包括停用条目；只返回短引用、标题和定位摘要 |
| `read_lore_entry` | `ref` | 按 L 引用完整读取一个可编辑条目的标题、正文、主／辅助关键词和启用状态 |
| `search_history` | `query`，`limit=5` | 当前分支剧情的字面检索；limit 为 1–10，`*` 列出最近记录，结果提供 H 引用 |
| `read_history` | `ref` | 读取一个有效 H 引用的完整原文，包含可用的当前用户输入 |
| `write_handoff` | `content` | 替换整个交接笔记，正文不能纯空白 |
| `new_context` | `content`，`keep_recent=2` | 替换交接笔记并换窗，必须单独调用 |
| `search_notes` | `query`，`limit=5` | 搜索当前分支的有效笔记；`*` 列出笔记，limit 为 1–10 |
| `read_note` | `ref` | 按完整笔记键读取有效或归档笔记 |
| `write_note` | `key`、`content`、`kind`，可选 `sources` | 新增或完整替换，无需预先读取笔记或历史；事实须有有效 H 来源 |
| `archive_note` | `key` | 从普通搜索中归档，仍可按键读取 |
| `read_state` | `path=""` | 点分路径，空值读取全部剧情状态；包含 MVU 时使用 `stat_data` 前缀 |
| `patch_state` | `operations`、`reason` | 先完整读取最新变量，再提交本轮授权的一次 MVU 补丁；路径相对于 `stat_data`，无变化提交 `[]`，成功后不可重复 |
| `read_character` | `ref` | 完整读取七个获准人物设定字段中的一个；缺失字段返回 null；不支持整卡、世界书或扩展读取 |
| `revise_content` | `operations`、`reason` | 受限设定动作：改人物字段，新增、修改或删除普通世界书条目；不接受 JSON Patch、路径或整卡替换 |
| `set_retrieval_query` | `query` | 保存下一轮自动检索线索，不写入剧情正文 |

查询文本最长 1000 字符；读取引用和笔记键最长 80 字符。笔记键可使用字母、数字、下划线、中文、斜线、连字符、空格和点。handoff 和笔记正文还限制为最多 64000 字符，补丁理由最多 1000 字符。正文同时受配置中的 token 上限约束，见[工具与记忆设置](../guides/full-agent.md#工具与记忆设置)。

`patch_state.operations` 接受 0–100 项 JSON Patch；`revise_content.operations` 接受 1–100 项受限设定动作。变量规则由运行时以 `<mvu_rules>` 完整快照按需追加，全部启用的 tracker_mvu 条目均参与，不走叙事检索筛选。遵循最新快照并用 read_state 核对当前值；规则文本不改变本轮权限或正文协议。

历史和笔记工具只按文本检索，不检索旧工具交换。叙事工具 `search_read_lore` 与编辑定位工具 `search_lore_entries` 分开：前者遵循检索准入和启停规则，后者包含停用的普通条目。L 编号可交给 `read_lore_entry`，每次只读取一个可编辑条目；定位摘要、叙事检索正文和上下文已有资料不授予编辑资格。

## 引用与笔记更新

`kind` 为 `fact`、`hypothesis` 或 `plan`。`fact` 必须提供当前分支有效的 H 原文引用，包括当前用户输入；最多 20 个。其他类型的来源可选，提供时同样校验引用有效性。无需本回合搜索或读取来源。程序直接解析消息身份并保存原文指纹，结论是否得到原文支持仍由模型判断。

例如，H7 在当前分支有效时，可直接将它用于事实笔记：

```json
{
  "key": "物品/戒指",
  "content": "艾琳把戒指借给用户，约定离港前归还。",
  "kind": "fact",
  "sources": ["H7"]
}
```

H7 只用于说明格式，实际调用必须使用当前分支有效的引用。如果 `物品/戒指` 已存在，可以直接提交完整新版；模型仍应保留有效信息，并按需要用 `read_note` 核对。覆盖归档笔记会重新激活该键。

handoff 的有效版本取最新运行时记忆更新与成功 `write_handoff` 结果中较晚者。失败调用中的新内容不构成当前记忆。

## 换窗参数

`new_context.keep_recent` 按剧情消息条数计数，范围 0–12，默认 2；保留边界向前扩展到完整交流，当前用户输入另行保留。旧工具交换整体退出，不能留孤立的 tool result。

```json
{
  "content": "当前在港口。用户保管借来的戒指；离港前须归还。下轮核对码头通行证。相关长期笔记：物品/戒指。",
  "keep_recent": 2
}
```

换窗继续同一回合，不重置调用额度。强制换窗期间只接受单独的 `new_context`；正文、其他工具及混合批次被拒绝，随后将原因交回模型。

## 变量与设定编辑

`read_state` 使用点分路径，MVU 变量带 `stat_data` 前缀，如 `stat_data.hp`。`patch_state` 使用相对于 `stat_data` 的 JSON Pointer，如 `/hp`，省略 `/stat_data`。调用前须在本回合当前窗口成功完整读取最新变量：`read_state(path="")` 或 `read_state(path="stat_data")`；局部或过期读取不满足要求，空补丁也须先读。

`read_character` 只接受 `name`、`description`、`personality`、`scenario`、`mes_example`、`system_prompt`、`post_history_instructions`。每次完整读取一个字段；缺失时返回 `text: null`，附加信息默认含 `exists`。`*`、根路径、`character_book`、开场白、扩展和脚本均不能读取。

`revise_content` 的受限动作与参数为：

| 动作 | 参数 | 行为 |
|---|---|---|
| `set_character` | `field`、文本 `value` | 设置一个获准人物字段；先用 read_character 读取；空串可清空可选字段，name 必须有效 |
| `add_lore` | `entry` | 新增普通叙事条目；必填 title/content，可选 keys/secondary_keys/enabled；默认启用并追加，不要求整本读取 |
| `update_lore` | `ref`、非空 `changes` | 修改已读的一个 L 条目；只接受 title/content/keys/secondary_keys/enabled，省略保留 |
| `remove_lore` | `ref` | 删除已读的一个普通 L 条目 |

例如分别读取和更新单条设定：

```json
{"ref": "L7"}
```

```json
{"operations": [{"op": "update_lore", "ref": "L7", "changes": {"content": "用户获准在港口过夜。"}}], "reason": "用户要求更新港口设定"}
```

身份、数组位置、旧字段别名及配置保留由程序处理，模型不能提交路径、ID、数组下标、扩展或整卡对象。支持包装卡、未包装卡及列表／字典世界书。条目短引用在删除前项后仍指向原条目；已删除引用不会复用。

人物修改、已有条目修改／删除须在本回合当前窗口完整读取最新目标。成功修改使该项读取资格失效，其他项保持有效；换窗或新回合须重读，失败续接保留有效的字段／条目登记。旧版整卡读取登记不授予新编辑资格。整批先校验、再应用，失败不部分修改。

只开放普通叙事条目，包含停用条目。`tracker_mvu`、`runtime_init`、传统 `[InitVar]`、执行脚本和相关配置均受保护；原字段／条目包含内嵌脚本、HTML 执行入口或 InitVar/MVU 执行指令时整项不能修改或删除，新内容也不得引入这些代码。无关字段、开场白、脚本和未知扩展原样保留，程序不会执行代码来判断可编辑性。

## 权限与错误

关闭的工具同时退出模型工具列表和运行时白名单。关闭 `read_state` 时 `patch_state` 不可用；设定编辑按类型检查依赖，关闭 `read_character` 只阻止人物修改，关闭 `read_lore_entry` 只阻止已有条目的修改和删除，新增普通条目不要求读取。工具目录的 `edit_capabilities` 显示三类能力。`patch_state` 的定义在本轮授权前后保持稳定，但执行时要求本轮勾选更新变量，并限制一次成功提交；缺少必要提交时，最终正文会被否决并要求补调。关闭它或 `read_state` 后，界面取消变量更新勾选，提交接口也会提前拒绝。

`revise_content` 受对话设定策略控制。预览策略下，包含设定变更的整份回合草稿等待审核。取消、失败或拒绝审核均不提交剧情状态。

参数 JSON 损坏时停止回合并保留已有进度；合法 JSON 中的字段、引用或权限错误作为工具结果返回，允许模型在调用额度内纠正。

参数校验失败时，`error` 明确指出缺少、多余、类型或范围错误的字段，`expected_parameters` 返回该工具接受的参数定义，供模型修正后重新调用。错误结果不回显输入值、Pydantic 内部类名或文档链接；校验通过前不执行工具。例如 `read_history` 收到 `reason` 而未收到 `ref`，会说明“缺少必填参数 ref；不接受参数 reason，请删除”，并返回必填字符串 `ref` 的定义。

## 返回字段与会话接口

用户在“工具”页选择返回字段，模型不能通过调用参数改变这些选项：

| 工具 | 可选字段与默认值 |
|---|---|
| `search_read_lore` | 编号、标题默认关闭；开启编号后可返回 `already_in_context` |
| `search_history`、`search_notes` | 摘要默认开启；空结果为 `matches: []` |
| `read_history`、`read_note`、`read_state`、`read_character` | 附加信息默认开启 |

工具返回正文完整进入上下文；界面分页只限制展示，不截断模型结果。

| 会话 API | 用途 |
|---|---|
| `GET/PUT /api/sessions/{sid}/full-agent/tools` | 读取工具定义及保存开关、返回字段；写入带 `expected_revision` |
| `GET /api/sessions/{sid}/full-agent` | 读取 handoff、笔记与窗口概况 |
| `PUT /api/sessions/{sid}/full-agent/context` | 人工保存 handoff；`new_window=true` 时换窗，`keep_recent` 默认 0 |

人工编辑需要已有剧情节点，正文非空且符合 handoff token 上限；写入携带 `expected_revision`，运行或待审核回合期间拒绝修改。活动记录接口与完整类型见 [full_agent/api.py](../../src/tavern/full_agent/api.py)，回归入口见 [test_full_agent_tools.py](../../tests/backend/test_full_agent_tools.py)。
