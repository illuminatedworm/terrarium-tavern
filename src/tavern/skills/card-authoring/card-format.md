# 角色卡数据格式

本项目按 Character Card V2 读取角色卡：`spec` 为 `chara_card_v2`，正文字段在 `data` 下。新草稿使用 V2；旧卡可能是未包装对象，以 `overview` 返回的根路径为准。

## 路径与编辑

- 包装卡路径为 `/data/<字段>`，未包装旧卡为 `/<字段>`。JSON Pointer 中 `~` 写作 `~0`，`/` 写作 `~1`；数组下标从 0 开始，`/-` 表示追加。读取结果中的 `@D` 短编号直接用在路径里，由程序恢复为原键。
- `add` 要求父节点存在：缺少 `extensions` 时先添加 `{}`，再写其中的键。
- 世界书 `entries` 可能是数组，也可能是以键索引的对象，编辑时保持原形态。新条目省略 `id`，由程序生成。

## 顶层字段

| 字段 | 类型 | 游玩时的去向 |
|---|---|---|
| `name` | 文本 | 角色名，`{{char}}` 展开为它 |
| `system_prompt` | 文本 | `<card_system_prompt>`，固定资料区第一块 |
| `description` | 文本 | `<card_description>`，紧随其后 |
| `personality` | 文本 | `<card_personality>`，描述之后 |
| `scenario` | 文本 | `<card_scenario>`，性格之后 |
| `mes_example` | 文本 | `<card_mes_example>`，用户角色资料之后、剧情之前 |
| `post_history_instructions` | 文本 | `<card_post_history_instructions>`，历史和当前输入之后 |
| `first_mes` | 文本 | 开场 0 |
| `alternate_greetings` | 文本数组 | 开场 1..N。用户只选一个开场作为第一条 assistant 剧情，其余开场不发送 |
| `character_book` | 对象 | 世界书，条目按下文规则准入 |
| `extensions` | 对象 | 只解释已支持的扩展：`regex_scripts`、Tavern Helper 脚本、世界书条目的 `terrarium.audience` 等 |
| `creator_notes`、`creator`、`tags`、`character_version` | 文本／数组 | 卡片信息，不进入游玩请求 |

非空字段才注入，宏展开后完整发送。标签只标识字段来源，运行时不附加用途说明，字段里写什么由作者决定。上表是默认顺序；Half Agent 可由会话配方调整，Full Agent 固定使用此顺序。固定资料区内容变化会让请求前缀失效，详见各模式文档。

## 宏

`{{char}}`、`{{user}}` 在发送时展开为角色名和用户角色名，写卡时保留字面形式。变量宏见 `runtime-guide.md`。未知宏原样保留。

## 世界书条目

一个条目的完整示例及追加操作：

```json
[{"op": "add", "path": "/data/character_book/entries/-", "value": {
  "comment": "人物·莉娅",
  "keys": ["莉娅", "红发剑士"],
  "content": "莉娅（红发剑士）：林默的旧日战友，现为追杀令的执行者。……",
  "enabled": true,
  "constant": false,
  "insertion_order": 100
}}]
```

运行时读取的字段：

| 字段 | 默认 | 作用 |
|---|---|---|
| `keys` | — | 主关键词，任一命中即成为候选 |
| `secondary_keys`、`selectiveLogic` | 空、0 | 次级关键词非空时追加条件：0 任一命中，1 并非全部命中，2 全部未命中，3 全部命中。V2 的 `selective` 只为其他前端保留，本项目以 `secondary_keys` 是否为空为准 |
| `content` | — | 正文，准入后整条注入 |
| `comment` | 空 | 条目标题，用于界面与诊断；含 `[InitVar]` 的条目只供初始化（见 `runtime-guide.md`） |
| `name` | 空 | 可选的主体名，参与下文的主体判断 |
| `enabled` | true | 关闭后不注入 |
| `constant` | false | 常驻条目，每轮注入，不经检索，不占新增名额 |
| `insertion_order` | 100 | 同级排序，数值小的在前 |
| `probability`、`useProbability` | 100、true | 命中后的注入概率（百分比） |
| `scan_depth` | 4 | 关键词扫描最近几条消息，另加当前输入 |
| `caseSensitive` | false | 区分大小写 |
| `matchWholeWords` | false | 按整词匹配。中文关键词前后通常是文字，开启后几乎无法命中 |
| `use_regex`，或写成 `/pattern/flags` 的关键词 | — | 正则关键词，使用简单模式 |
| `excludeRecursion`、`preventRecursion` | false | 不被递归激活；正文不激活其他条目 |
| `position`、`depth`、`role` | — | 注入位置，生效方式见各模式文档 |
| `extensions.terrarium.audience` | `writer` | 条目用途：`writer`、`tracker_mvu` |

其他前端导出的这些字段常放在条目的 `extensions` 中，运行时合并读取，原样保留即可。`matchWholeWords`、`excludeRecursion`、`preventRecursion`、`caseSensitive` 也接受 SillyTavern 的 `match_whole_words`、`exclude_recursion`、`prevent_recursion`、`case_sensitive` 写法，顶层和 `extensions` 均可读取。已有 camelCase 优先，同一拼写以 `extensions` 为先，`false` 和 `null` 不回退到别名（`null` 按默认关闭处理）。修改时保留已有字段位置与拼写，不添加相互矛盾的同义字段；有多个同义字段时应同步修改。

导出角色卡时，Runtime 按上述优先级保留这四个开关的生效值，只输出 `extensions` 下的 snake_case 字段，清除顶层和扩展中的重复别名；未配置的字段不补默认值。本地保存的原稿不受导出转换影响。

## 召回规则对写法的影响

1. 关键词扫描最近消息与当前输入；BM25 和可选的向量通道也提供候选，最终都须通过准入。
2. 主体判断：命中的关键词都不超过 2 个字，并且都不出现在 `name` 或正文第一行时，条目不注入。正文第一行写明主体及常用称呼，例如“莉娅（红发剑士）：……”。
3. 单个英文字母的关键词需要开启 `matchWholeWords`；1–2 位英文或数字按整词匹配。
4. 每轮默认最多新增 5 条，常驻条目不计入。条目整条注入，过长只会多占 token。
5. 递归默认一层：已准入条目正文里的关键词可以再激活条目，仍须与当前情境相关。
6. 已注入的条目随所在对话保留，这段对话退出窗口后才进入冷却。
