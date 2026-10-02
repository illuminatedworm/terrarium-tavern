# Full Agent 工具协议

工具运行在本回合草稿上。成功的工具结果表示草稿已接受，正文、脚本处理和审核完成后才提交到剧情。工具开关、操作步骤见[使用指南](../guides/full-agent.md)，窗口与恢复机制见[架构](../design/full-agent.md)。

## 工具目录

参数由 [full_agent/tools.py](../../src/tavern/full_agent/tools.py) 中的类型定义生成，预览和实际请求使用同一生成器。参数对象拒绝未定义字段。

| 工具 | 参数 | 结果与约束 |
|---|---|---|
| `search_read_lore` | `query` | 使用世界书索引和准入规则，直接返回完整正文；已在上下文的条目省略正文 |
| `search_history` | `query`，`limit=5` | 当前分支剧情的字面检索；limit 为 1–10，`*` 列出最近记录，结果提供 H 引用 |
| `read_history` | `ref` | 读取一个有效 H 引用的完整原文，包含可用的当前用户输入 |
| `write_checkpoint` | `content` | 替换整个交接笔记，正文不能纯空白 |
| `new_context` | `content`，`keep_recent=2` | 替换交接笔记并换窗，必须单独调用 |
| `search_notes` | `query`，`limit=5` | 搜索当前分支的有效笔记；`*` 列出笔记，limit 为 1–10 |
| `read_note` | `ref` | 按完整笔记键读取有效或归档笔记 |
| `write_note` | `key`、`content`、`kind`，可选 `sources` | 新增或完整替换；覆盖前必须在本回合当前窗口读取现有版本 |
| `archive_note` | `key` | 从普通搜索中归档，仍可按键读取 |
| `read_state` | `path=""` | 点分路径，空值读取全部剧情状态；包含 MVU 时使用 `stat_data` 前缀 |
| `patch_state` | `operations`、`reason` | 本轮授权后提交一次 MVU 补丁；路径相对于 `stat_data` |
| `read_character` | `ref` | 读取规范化角色数据的一个顶层字段，如 `description` 或 `character_book` |
| `revise_content` | `operations`、`reason` | 修改授权的对话级角色卡／世界书草稿 |
| `set_retrieval_query` | `query` | 保存下一轮自动检索线索，不写入剧情正文 |

查询文本最长 1000 字符；读取引用和笔记键最长 80 字符。笔记键可使用字母、数字、下划线、中文、斜线、连字符、空格和点。checkpoint 和笔记正文还限制为最多 64000 字符，补丁理由最多 1000 字符。正文同时受配置中的 token 上限约束，见[工具与记忆设置](../guides/full-agent.md#工具与记忆设置)。

历史和笔记工具只按文本检索，不检索旧工具交换。世界书没有独立 `read_lore` 工具；可选的 L 编号仅标识搜索结果和已经在场的资料，不是读取参数。

## 引用与笔记更新

`kind` 为 `fact`、`hypothesis` 或 `plan`。`fact` 必须提供本回合实际搜索或读取过的 H 原文引用；最多 20 个。程序校验来源身份与指纹，结论是否得到原文支持仍由模型判断。

例如，搜索到 H7 并读取其原文后，才能将它用于事实笔记。以下各对象分别是一次工具调用的参数：

```json
{"ref": "H7"}
```

```json
{
  "key": "物品/戒指",
  "content": "艾琳把戒指借给旅人，约定离港前归还。",
  "kind": "fact",
  "sources": ["H7"]
}
```

H7 只用于说明格式，实际调用必须使用当前分支返回的引用。如果 `物品/戒指` 已存在，先调用 `read_note` 读取该键，再提交包含仍有效信息的完整新版。搜索摘要、旧版内容及换窗前的读取不满足覆盖条件。覆盖归档笔记会重新激活该键。

checkpoint 的有效版本取最新运行时记忆更新与成功 `write_checkpoint` 结果中较晚者。失败调用中的新内容不构成当前记忆。

## 换窗参数

`new_context.keep_recent` 按剧情消息条数计数，范围 0–12，默认 2；保留边界向前扩展到完整交流，当前用户输入另行保留。旧工具交换整体退出，不能留孤立的 tool result。

```json
{
  "content": "当前在港口。旅人保管借来的戒指；离港前须归还。下轮核对码头通行证。相关长期笔记：物品/戒指。",
  "keep_recent": 2
}
```

换窗继续同一回合，不重置调用额度。强制换窗期间只接受单独的 `new_context`；正文、其他工具及混合批次被拒绝，随后将原因交回模型。

## 状态与角色资料路径

三类工具的路径根不同：

| 操作 | 示例 | 说明 |
|---|---|---|
| `read_state` | `{"path":"stat_data.hp"}` | 点分读取路径，可用数字段读取数组；缺失路径返回 `null` |
| `patch_state` | `{"operations":[{"op":"replace","path":"/hp","value":8}],"reason":"本轮受伤"}` | JSON Pointer 相对于 `stat_data`，省略 `/stat_data` |
| `read_character` | `{"ref":"description"}` | 顶层字段名，省略 `data`，不接受点分路径或 JSON Pointer |
| `revise_content` | `{"operations":[{"op":"replace","path":"/data/description","value":"修订后的完整描述"}],"reason":"用户授权修改"}` | 相对于原始卡片；包装卡使用 `/data`，未包装卡省略它 |

修改前先读取对应值。JSON Patch 支持 `add/remove/replace/move/copy/test`；`add/replace/test` 必须带 `value`，`move/copy` 必须带 `from`。路径中的 `~` 写为 `~0`、`/` 写为 `~1`，数组追加使用 `/-`。Full Agent 的补丁数组要求 1–100 项；表达变量未变化时可对已读值使用 `test`，不能照搬 Half Agent 的空数组提交方式。

`read_state` 的点分路径不能转义键名中的字面点；遇到这种结构应读取其父对象。角色工具描述会列出工具上下文开始时的实际字段和原始卡片根前缀，修改后字段可能变化。

## 权限与错误

关闭的工具同时退出模型工具列表和运行时白名单。`patch_state` 的定义保持稳定，但执行时要求本轮勾选更新变量，并限制一次成功提交；缺少必要提交时，最终正文会被否决并要求补调。手动关闭它后，界面不能勾选变量更新。

`revise_content` 受对话设定策略控制。预览策略下，包含设定变更的整份回合草稿等待审核。取消、失败或拒绝审核均不提交剧情状态。

参数 JSON 损坏时停止回合并保留已有进度；合法 JSON 中的字段、引用或权限错误作为工具结果返回，允许模型在调用额度内纠正。

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
| `GET /api/sessions/{sid}/full-agent` | 读取 checkpoint、笔记与窗口概况 |
| `PUT /api/sessions/{sid}/full-agent/context` | 人工保存 checkpoint；`new_window=true` 时换窗，`keep_recent` 默认 0 |

人工编辑需要已有剧情节点，正文非空且符合 checkpoint token 上限；写入携带 `expected_revision`，运行或待审核回合期间拒绝修改。活动记录接口与完整类型见 [full_agent/api.py](../../src/tavern/full_agent/api.py)，回归入口见 [test_full_agent_tools.py](../../tests/backend/test_full_agent_tools.py)。
