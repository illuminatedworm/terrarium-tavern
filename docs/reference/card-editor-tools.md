# Card Editor 工具与接口

工具运行在本轮未提交进度上，完整结束后共同更新独立卡片草稿及用户明确要求的用户角色；模型不能保存书架卡片，用户角色修改则直接保存到用户角色库。参数由 [editor_tools.py](../../src/tavern/character/editor_tools.py) 的严格类型生成，实际请求、预览和运行时校验共用目录。

## 卡片与历史引用

`C1` 等编号只属于当前制卡对话，编辑后仍指向同一卡的最新草稿，移除或丢弃后不复用。`H1` 等编号定位本对话用户输入和最终回复；确认方案由程序读取引用原文。文档内部 `@D` 编号使用可逆映射，真实身份、指纹及哈希由程序管理。

用户角色使用 P 编号，按请求访问后由程序分配；与 C 编号共用递增计数，撤销或丢弃后不复用。模型不接触用户角色存储 ID 或填写版本号。用户角色工具必填 request_ref，指向本任务最新用户请求；程序核对来源和当前任务，具体操作意图由 Agent 根据原文判断。旧回合、助手方案、资料中的指令不能作为操作依据，只读要求和撤销要求必须遵守。

工具没有全书架检索、任意文件、shell 或网络浏览能力。引用由用户通过界面加入；来源变化不会自动覆盖草稿。

## 工具目录

| 工具 | 参数与结果 |
|---|---|
| `overview` | 可选 `card_ref`；查看当前目录或指定卡的根路径、格式、字段用途/类型/大小 |
| `read_field` | `card_ref`、JSON Pointer `path`；空路径读整卡。`version=current/round_start/saved` 分别读取当前、任务开始前或书架基准，始终返回完整原文 |
| `search_entries` | `card_ref`、`query`；空查询列出全部。提供标题、定位路径及明确标识的摘要，修改前读取全文 |
| `create_draft` | `name`、可选 `source_ref`、`proposal_ref`、`confirmation_ref`；明确直接制作或机械复制使用 `direct_instruction=true`。返回新引用与初始文档 |
| `configure_card` | `card_ref`、`target_mode=half_agent/full_agent`、用户选择的 `confirmation_ref`；可选兼容性目标，普通制卡和 MVU 无需调用，不自动迁移内容 |
| `patch_draft` | `card_ref`、`operations`、`reason`；严格 JSON Patch，一批 1–100 项原子应用 |
| `revert_draft` | `card_ref`、`reason`；程序恢复本轮开始前的整张卡，新创建的卡则从本轮移除，其他卡保持不变 |
| `set_opening` | `card_ref`、`action=update/add/remove/move`、`index`、可选 `narrative/initialization/to_index`；程序维护关联、身份和指纹 |
| `validate_draft` | `card_ref`；分别返回结构错误、完整性提示和静态兼容性信息 |
| `test_draft` | `card_ref`、`index=0`、可选完整文本 `samples`、`display_assertions`、`state_patch`；检查初始化、正则转换、脚本启动和指定 DOM 值，临时补丁用于验证更新后显示，返回实际结果、日志和未测范围 |
| `read_skill` | 白名单 `resource`：card-guide.md、runtime-guide.md、half-agent.md、full-agent.md、SKILL.md、references/workflow.md、references/status-display.md、references/regex-display.md；状态栏迁移参考文档覆盖 HTML、Markdown 与固定标签展示 |
| `write_handoff` | 完整替换 `content`；非空且符合配置 token 上限，不取代确认方案原文 |
| `new_context` | 保存 `content` 后换窗，必须单独调用；继续当前任务且不重置批次调用额度 |
| `search_history` | `query`、`limit=5`（1–20）；`*` 列出最近记录，返回 H 引用与定位摘要 |
| `read_history` | `ref`；完整读取消息及关联工具交换，不把原生 reasoning 或协议 ID 作为业务正文返回 |
| `list_personas` | `request_ref`、可选名称过滤 `query`；只返回名称和 P 编号，不自动读取介绍 |
| `read_persona` | `request_ref`、`persona_ref`；完整读取当前名称和介绍，修改前必须读取；并发变化时提示旧待保存修改 |
| `create_persona` | `request_ref`、`name`、`description`；新建仅包含名称和介绍的用户角色，正常完成后保存 |
| `update_persona` | `request_ref`、`persona_ref`、可选 `name/description`；省略保留，description 空串清空；`revert=true` 仅撤销本任务待保存修改 |

## 路径与修改

路径相对于完整文档。包装卡使用 `/data/description`，旧卡使用 `/description`。键中的 `~` 转义成 `~0`，`/` 转义成 `~1`；数组下标从 0 开始，`/-` 追加。

`add/replace/test` 必须带 `value`，即使值为 null；`move/copy` 必须带 `from`。修改前需完整读取对应当前子树；读取 round_start/saved 不等同于读取当前版本。新增卡返回的完整初始文档视为已读。未知参数、无效引用和非法操作反馈给模型纠正；失败的批次不改变文档。

开场编号 0 对应 first_mes，其余对应 alternate_greetings。初始化参数省略时保留现有关联；仅更新初始化时可以省略 narrative，由程序保留原文。新增开场需要完整 narrative；删除最后一个开场应改为空文本。普通编辑及可见状态栏迁移不自动提取有效的内联初始化；已正确初始化并对模型隐藏的数据须原位保留。独立 initialization 是可选的项目扩展，首次创建关联时程序复制其他开场已有的内联 initvar，以免空记录覆盖其有效初值；既有记录与内联数据并存时诊断会提示核对实际来源。

本地检查使用临时状态、内嵌和已缓存依赖，超时上限 30 秒；不会发起模型请求。正则字段的类型、缺少 findRegex/replaceString、HTML 误入模型通道会给出静态警告。正则无规则时返回 skipped，有规则时仅报告 observed 转换结果，不等于界面验证。

`display_assertions` 的每项含 `sample_index`（0 为所选开场，1..N 为 samples）、CSS `selector`、期望 `text` 和可选 `after_text`。工具在禁止联网的独立页面中加载实际展示 HTML 和兼容 SDK，要求选择器唯一且完整 textContent（去除首尾空白）一致。`state_patch` 为相对 stat_data 的 JSON Patch，通过 Runtime 应用到临时快照后重建页面检查 after_text；不改变草稿、故事或权限。缺少断言时明确报告前端未测，缺少更新断言时报告更新后的显示未测。完整用例见内置 references/regex-display.md。

passed、failed、blocked 及 untested 各有含义，不能用 passed 代表全部动态和叙事效果已验证。

初始化诊断还提示损坏的 initvar 标签、被误标为 tracker_mvu 的 [InitVar] 条目、工作流记录覆盖内联数据及需要单独验证的开场增量指令；[InitVar] 数据不因含结构标记而误报为 Writer 更新规则。

静态诊断对角色字段、启用世界书、开场及示例中的疑似逐轮状态栏输出、根路径变量宏、静态状态示范和缺少声明式初值/变量规则给出带位置的提示。提示不阻止草稿编辑或保存，不自动删除 HTML，也不替代语义判断；未发现警告及内部 adapted 标志均只表示通过已实现的静态检查，不证明完整适配。停用条目不触发状态栏提示，传统禁用的 [InitVar] 条目仍按运行契约识别。

用户角色名称最多 100 字符且不能纯空白，介绍完整读写，不分页或截断。用户角色写入与任务完成在同一事务中提交，并检查读取时的保存版本；任何冲突均不部分保存。失败续接不会重复创建角色，已有故事身份快照保持独立。不提供删除用户角色的工具。

## HTTP 接口

| 入口 | 用途 |
|---|---|
| `GET/POST /api/card-editor/conversations` | 列表及空对话创建 |
| `GET/PUT/DELETE /api/card-editor/conversations/{id}` | 读取、改名、删除 |
| `POST .../{id}/references` | 引用书架卡，可携带当前表单和原卡版本 |
| `DELETE .../{id}/references/{ref}` | 移除该卡的引用、草稿和撤销历史 |
| `PUT .../{id}/drafts/{ref}` | 手工修改文档；target_mode 省略保留兼容性目标，null 清除目标 |
| `POST .../{id}/drafts/{ref}/save` | `mode=create/overwrite/copy`，携带 operation_id |
| `POST .../{id}/drafts/{ref}/undo`、`reload`、`test` | 撤销、重新载入、运行本地检查 |
| `POST .../{id}/runs` | 发送；`resume_run_id` 续接，非空 text 表示补充要求 |
| `DELETE .../{id}/progress` | 丢弃本轮未提交进度 |
| `GET .../{id}/context`、`tools`；`POST .../{id}/preview` | 上下文状态、工具说明和只读请求预览 |
| `/api/card-edit-runs/{id}/activity`、`events`、`cancel` | 动作分页、可重连 SSE 和停止 |

变更接口使用 `expected_revision`；运行/待续任务阻止修改其基础。保存与发送使用 operation_id 幂等。旧 `/api/card-edits` 接口仅适配单卡，不在多卡对话中猜测目标。原文与完整工具交换独立保存；activity 的 4000 字符摘要只限制用户日志显示。
