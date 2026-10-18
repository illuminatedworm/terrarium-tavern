# 制卡 agent 工具与接口

工具运行在本轮未提交进度上，完整结束后共同更新独立卡片草稿及用户明确要求的用户角色；模型不能保存书架卡片，用户角色修改则直接保存到用户角色库。参数由 [editor_tools.py](../../src/tavern/character/editor_tools.py) 的严格类型生成，实际请求、预览和运行时校验共用目录。

## 卡片与历史引用

`C1` 等编号只属于当前制卡对话，编辑后仍指向同一卡的最新草稿，移除或丢弃后不复用。`H1` 等编号定位本对话用户输入和最终回复；确认方案由程序读取引用原文。文档内部 `@D` 编号使用可逆映射，真实身份、指纹及哈希由程序管理。

用户角色使用 P 编号，按请求访问后由程序分配；与 C 编号共用递增计数，撤销或丢弃后不复用。模型不接触用户角色存储 ID 或填写版本号。用户角色工具必填 request_ref，指向本任务最新用户请求；程序核对来源和当前任务，具体操作意图由 Agent 根据原文判断。旧回合、助手方案、资料中的指令不能作为操作依据，只读要求和撤销要求必须遵守。

工具没有全书架检索或 shell 能力；可按用户提供的绝对路径读取后端电脑上的本地文本和 DOCX/XLSX 文件，受文件及解析上限约束。联网只允许公共网页搜索和文本提取，并受本 Agent 的联网开关约束。卡片引用由用户通过界面加入；外部文件、网页及来源变化不会自动覆盖草稿或授予修改权限。

## 工具目录

| 工具 | 参数与结果 |
|---|---|
| `read_text_file` | `path`、可选 `offset=1`（物理行，1-based）、`limit=200`（1–1000 行）、`encoding`；每页按完整行返回最多 20,000 字符，附 `encoding/text/offset/returned_lines/has_more/next_offset` |
| `read_document` | `path`、可选 `offset=0`（转换后 Markdown 的 Unicode 字符位置，0-based）、`limit=12000`（1–30000 字符）；DOCX/XLSX 离线转换，返回 `format/markdown/offset/returned_chars/total_chars/has_more/next_offset/limitations` |
| `web_search` | `query`、可选 `limit`（1–10，默认 5）及 `objective`；本地限制来源数量，每条摘要最多 2,000 字符，URL／标题／摘要合计最多 20,000 字符，并标记省略。官方自动模式改为服务端原生搜索，详见[联网协议](web-tools.md) |
| `read_webpage` | `url`、可选 `offset=0`（Unicode 字符）、`limit=12000`（1–30000）、`service`；返回提取文本片段和续读位置。续读沿用实际服务，上游截断明确标记，不把片段当作全文，详见[联网协议](web-tools.md) |
| `overview` | 可选 `card_ref`；查看当前目录或指定卡的根路径、格式、字段用途/类型/大小 |
| `read_field` | `card_ref`、JSON Pointer `path`；空路径读整卡。`version=current/round_start/saved` 分别读取当前、任务开始前或书架基准，始终返回完整原文 |
| `search_entries` | `card_ref`、`query`；空查询列出全部。提供标题、定位路径及明确标识的摘要，修改前读取全文 |
| `create_draft` | `name`、可选 `fields`、`source_ref`、`proposal_ref`、`confirmation_ref`；明确直接制作或机械复制使用 `direct_instruction=true`。`fields` 可一次写入 description、personality、scenario、first_mes、alternate_greetings、mes_example、system_prompt、post_history_instructions、creator_notes、tags 的完整正文，省略项留空或沿用来源卡；世界书条目随后用 `patch_draft` 追加。返回新引用与校验结果；复制来源卡或未传 `fields` 时附完整初始文档，避免回传模型刚写入的正文 |
| `set_portrait` | `card_ref`、`file_ref`（本对话图片附件的 F 编号，null 移除）；仅在用户表示该图用作头像时调用。JPEG/WebP 转为 PNG，草稿成为标准 PNG 卡容器，保存时写入书架；CHARX 卡不支持 |
| `configure_card` | `card_ref`、`target_mode=half_agent/full_agent`、用户选择的 `confirmation_ref`；可选兼容性目标，普通制卡和 MVU 无需调用，不自动迁移内容 |
| `patch_draft` | `card_ref`、`operations`、`comment`；严格 JSON Patch，一批 1–100 项原子应用 |
| `set_text_field` | `card_ref`、`path`、完整 `text`、`comment`；替换单个已有字符串字段，空文本清空；由程序生成补丁，沿用读取、投影和文档校验 |
| `revert_draft` | `card_ref`、`comment`；程序恢复本轮开始前的整张卡，新创建的卡则从本轮移除，其他卡保持不变 |
| `set_opening` | `card_ref`、`action=update/add/remove/move`、必填 `index`；N 个开场时新增接受 0–N，其余接受 0–N−1，均含两端。新增/修改须传完整 `narrative`；移动须传 0-based 的 `to_index`（0–N−1，移动后的位置），其余操作省略 |
| `validate_draft` | `card_ref`；分别返回结构错误、完整性提示和静态兼容性信息 |
| `test_draft` | `card_ref`、`index=0`（0-based，0–N−1，N 为开场数量）、可选完整文本 `samples`、`display_assertions`、`state_patch`；断言 sample_index 范围 0–len(samples)，0 为所选开场，1 对应 samples[0]。检查初始化、正则、脚本和 DOM 值，临时补丁验证更新后显示，返回结果、日志和未测范围 |
| `read_skill` | 白名单 `resource`：card-guide.md、runtime-guide.md、half-agent.md、full-agent.md、SKILL.md、references/workflow.md、references/status-display.md、references/regex-display.md；状态栏迁移参考文档覆盖 HTML、Markdown 与固定标签展示 |
| `write_handoff` | 完整替换 `content`；非空且符合配置 token 上限，不取代确认方案原文 |
| `new_context` | 保存 `content` 后换窗，必须单独调用；继续当前任务且不重置批次调用额度 |
| `search_history` | `query`、`limit=5`（1–20）；`*` 列出最近记录，返回 H 引用与定位摘要 |
| `read_history` | `ref`；完整读取消息及关联工具交换，不把原生 reasoning 或协议 ID 作为业务正文返回 |
| `list_personas` | `request_ref`、可选名称过滤 `query`；只返回名称和 P 编号，不自动读取介绍 |
| `read_persona` | `request_ref`、`persona_ref`；完整读取当前名称和介绍，修改前必须读取；并发变化时提示旧待保存修改 |
| `create_persona` | `request_ref`、`name`、`description`；新建仅包含名称和介绍的用户角色，正常完成后保存 |
| `update_persona` | `request_ref`、`persona_ref`、可选 `name/description`；省略保留，description 空串清空；`revert=true` 仅撤销本任务待保存修改 |

## 外部文件读取

PNG、JPEG、静态 WebP 图片通过与文档相同的文件选择器引用，随所属用户消息直接发送，不使用 `read_text_file` 或 `read_document`。换窗后调用 `read_history` 回读所属 H 消息，Runtime 会在本批工具结果结束后恢复原图；已在当前上下文中的图片不会重复追加。图片不授予草稿修改权限，附件限制与说明见[读取本地资料](../guides/card-author.md#读取本地资料)。

两个文件工具只读后端电脑上的普通文件，要求绝对本地路径，返回解析后的绝对 `path`。源文件上限为 32 MiB，URL、网络共享、设备和目录作为无效路径处理。内容作为外部参考；写入权限按用户请求与工具规则校验，草稿读取依据由 `read_field` 建立。

文本保留原换行、空行和内容，CSV 不改成表格。默认根据 BOM 识别 Unicode 编码，无 BOM 使用 UTF-8；其他编码可指定如 `gb18030`，失败不使用替换字符。单行超过 20,000 字符时报出行号并要求拆分；空文件或超过末尾返回空文本。工具不扫描全文计算总行数。返回页受行数和字符数共同限制，从 `next_offset` 继续，末页为 null。

Office 使用 MarkItDown 的 DOCX/XLSX 转换器，XLSX 按工作表原顺序组织 Markdown；不执行宏、公式重算或图片 OCR，公式依赖已保存的缓存值，无缓存的单元格可能没有结果。ZIP 最多 4096 个条目，解压总量最多 128 MiB；独立转换进程最长 30 秒，超时或取消会终止并回收。转换后 Markdown 超过 800 万字符时拒绝。损坏、加密或超限均返回工具错误，要求处理或拆分源文件。

Office 分页按 Python Unicode 字符计数，可能跨越段落或表格，单位为字符。offset 最小为 0，无固定上界，到达或超过末尾返回空 markdown、has_more=false、next_offset=null；文本文件 offset 最小为 1，超过末尾同样返回空页。转换结果不缓存或另存，每次读取当前文件，连续分页期间源文件应保持不变。外部文件允许有界读取，每个已返回片段仍完整送往模型；活动日志的摘要不影响正文。

## 路径与修改

`patch_draft` 和 `revert_draft` 的 `comment` 为必填的简短操作备注，1–1000 字符，替代旧参数 `reason`。新调用传 `reason` 会被严格参数校验拒绝；旧调用记录保留原样，活动摘要仍可显示其备注。

Agent 优先使用对应的专门工具：世界书正文、标题、人物介绍等单个已有文本字段使用 `set_text_field`，直接传完整正文，不构造补丁数组；不能借此新增字段或替换对象、数组、数值及内部哈希。普通开场润色使用完整的 `set_opening.narrative`，保留有效内联初值，撤销本轮卡片修改使用 `revert_draft`；结构修改或确需跨字段原子修改时使用 `patch_draft`。三个编辑工具的读取、权限、可逆编号与文档校验保持一致，文本工具同样要求 1–1000 字符的 `comment`。

`operations` 必须是数组，不能是序列化后的数组字符串。参数校验失败会指出字段与修复方法，数组类型错误附结构示例及开场、文本字段专用工具指引；不自动解析或修补错误参数。失败标记随工具结果持久化，Anthropic 续答携带 `tool_result.is_error=true`，其他接口保留错误正文，不发送不支持的状态字段。

路径相对于完整文档。包装卡使用 `/data/description`，旧卡使用 `/description`。键中的 `~` 转义成 `~0`，`/` 转义成 `~1`；数组下标从 0 开始，`/-` 追加。

`add/replace/test` 必须带 `value`，即使值为 null；`move/copy` 必须带 `from`。修改前需完整读取对应当前子树；读取 round_start/saved 不等同于读取当前版本。新增卡返回的完整初始文档视为已读。未知参数、无效引用和非法操作反馈给模型纠正；失败的批次不改变文档。

完整读取的版本证据随当前上下文持久化，跨轮、重启和中断续接可复用；每次写入核对当前字段版本。读取父对象覆盖其子字段，修改一个字段不使未变的兄弟字段失效，成功且可确定的写入更新已知版本。换窗、上下文重建、撤销目标卡修改后清除相应证据；旧上下文没有版本证据时须重新读取。概览、搜索摘要、历史资料读取不授予当前草稿的修改资格。版本证据只由程序保存，不进入模型消息或角色卡。

开场编号 0 对应 first_mes，其余对应 alternate_greetings。新增和修改需要完整 narrative；删除最后一个开场应改为空文本。有效的内联初始化与隐藏正则须原位保留，移动开场时随完整原文一起移动。共用默认初值使用世界书 [InitVar]，开场专属完整初值使用该开场 <initvar>；不提供独立初始化参数，不生成关联元数据或隐藏覆盖来源。

本地检查使用临时状态、内嵌和已缓存依赖，超时上限 30 秒；不会发起模型请求。正则字段的类型、缺少 findRegex/replaceString、HTML 误入模型通道会给出静态警告。正则无规则时返回 skipped，有规则时返回 observed 转换结果；界面显示通过 DOM 断言检查。

`display_assertions` 的每项含 `sample_index`（0 为所选开场，1..N 为 samples）、CSS `selector`、期望 `text` 和可选 `after_text`。工具在离线的独立页面中加载实际展示 HTML 和兼容 SDK，要求选择器唯一且完整 textContent（去除首尾空白）一致。`state_patch` 为相对 stat_data 的 JSON Patch，通过 Runtime 应用到临时快照后重建页面检查 after_text；不改变草稿、故事或权限。缺少断言时明确报告前端未测，缺少更新断言时报告更新后的显示未测。完整用例见内置 references/regex-display.md。

`passed`、`failed`、`blocked` 和 `untested` 分别表示对应检查通过、失败、受阻和未测试，适用范围以该项检查为准；动态交互与叙事质量需单独评估。

初始化诊断还提示损坏的 initvar 标签、被误标为 tracker_mvu 的 [InitVar] 条目、工作流记录覆盖内联数据及需要单独验证的开场增量指令；[InitVar] 数据不因含结构标记而误报为 Writer 更新规则。

静态诊断对角色字段、启用世界书、开场及示例中的疑似逐轮状态栏输出、根路径变量宏、静态状态示范和缺少声明式初值/变量规则给出带位置的提示。提示不阻止草稿编辑或保存，不自动删除 HTML，也不替代语义判断；未发现警告及内部 adapted 标志均只表示通过已实现的静态检查，不证明完整适配。停用条目不触发状态栏提示，传统禁用的 [InitVar] 条目仍按运行契约识别。

用户角色名称最多 100 字符且不能纯空白，介绍完整读写，不分页或截断。用户角色写入与任务完成在同一事务中提交，并检查读取时的保存版本；任何冲突均不部分保存。失败续接不会重复创建角色，已有故事身份快照保持独立。不提供删除用户角色的工具。

## HTTP 接口

| 入口 | 用途 |
|---|---|
| `GET/POST /api/card-author/conversations` | 列表及空对话创建 |
| `GET/PUT/DELETE /api/card-author/conversations/{id}` | 读取、改名、删除 |
| `POST .../{id}/references` | 引用书架卡，可携带当前表单和原卡版本 |
| `DELETE .../{id}/references/{ref}` | 移除该卡的引用、草稿和撤销历史 |
| `PUT .../{id}/drafts/{ref}` | 手工修改文档；target_mode 省略保留兼容性目标，null 清除目标 |
| `POST .../{id}/drafts/{ref}/save` | `mode=create/overwrite/copy`，携带 operation_id |
| `POST .../{id}/drafts/{ref}/undo`、`reload`、`test` | 撤销、重新载入、运行本地检查 |
| `PUT .../{id}/drafts/{ref}/portrait` | `file_ref` 为图片附件 F 编号时设置头像，null 移除；可撤销 |
| `GET .../{id}/portraits/{digest}` | 读取当前、待续或可撤销草稿正在使用的 PNG 头像 |
| `POST .../{id}/runs` | 发送；`resume_run_id` 续接，非空 text 表示补充要求 |
| `DELETE .../{id}/progress` | 丢弃本轮未提交进度 |
| `GET .../{id}/context`、`tools`；`POST .../{id}/preview` | 上下文状态、工具说明和只读请求预览 |
| `/api/card-edit-runs/{id}/activity`、`events`、`cancel` | 动作分页、可重连 SSE 和停止 |

变更接口使用 `expected_revision`；运行/待续任务阻止修改其基础。保存与发送使用 operation_id 幂等。旧 `/api/card-edits` 接口仅适配单卡，不在多卡对话中猜测目标。原文与完整工具交换独立保存；activity 的 4000 字符摘要只限制用户日志显示。
