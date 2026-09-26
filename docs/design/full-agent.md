# Full Agent 模式

创建故事时选择 **Half Agent** 或 **Full Agent**。旧会话默认 Half Agent，分支继承原模式；不在已有故事中直接切换模式，避免两套记忆与窗口相互解释。

Full Agent 是独立的单模型工具循环：它生成故事，也决定何时检索、记什么、读什么、何时换窗。`full_agent/` 不调用 Writer、Tracker、旧窗口规划器、分类记忆引擎或旧上下文编译器。`application/modes.py` 仅按模式路由。两种模式共享 SQLite、模型 HTTP 适配器、角色资料、世界书索引和隔离脚本执行器等底层能力。

## 两种模式的职责

| 工作 | Half Agent | Full Agent |
|---|---|---|
| 正文 | 无工具 Writer | 唯一 Agent 的无工具最终回复 |
| 记忆 | Tracker 周期整理、裁剪前整理 | Agent 主动调用 checkpoint / note 工具 |
| 窗口 | Runtime 规划、裁剪及保护记忆覆盖 | Agent 主动 `new_context`；Runtime 只计数、提醒、拒绝超预算请求 |
| 世界书 | 自动检索、中途注入 | 相同自动检索与注入，另有模型自拟 query 工具 |
| 变量 | 独立后台模型提出 MVU 操作 | 同一 Agent 调用 `patch_state` |
| 设定维护 | 授权 Tracker | 同一 Agent 调用 `revise_content` |
| 模型配置 | foreground / background | 可独立配置 full_agent；未配置时仅沿用 foreground 的连接和预算 |

Full Agent 有独立系统提示词和调用预算。沿用模型配置不意味着调用 Writer。Card Editor 仍属于书架编辑功能，不参与游玩回合。

## 工作窗口

参考 [OpenAI 官方 experimental context management 说明](https://learn.chatgpt.com/docs/models?surface=app#experimental-context-management)：模型维护跨窗口笔记，并搜索同一任务的早期消息与工具结果。公开说明未提供可直接移植的完整服务协议。以下工具、存储与事务规则是本项目实现，不调用 ChatGPT 私有 notes/history 端点，也不使用服务端 compaction。

窗口包含基础规则、角色与玩家、常驻世界书、当前 checkpoint、窗口内正文与工具交换、按锚点保留的世界书，以及当前输入、授权和预算提醒。Half Agent 的上下文配方、历史 cap、周期记忆设置不影响该窗口。

`new_context(content, keep_recent)`：

1. 校验非空 checkpoint、参数和预算。校验失败时不换窗。
2. 在回合草稿中一起替换 checkpoint 和窗口游标。
3. 保留最近完整交流，当前用户输入始终保留；旧工具交换整体退出，不能留下孤立 tool result。
4. 同一个 Agent 继续当前回合。换窗不重置回合调用额度。
5. 正文成功后，checkpoint、笔记、变量、窗口、检索与工具记录一并提交。

不做自动摘要、不启动整理模型、不静默截断历史。每次请求计算工具定义、封装、结果及传输续答所需 reasoning 字段的占用；软阈值提醒模型提前换窗，硬上限明确停止并保留旧状态。用户可在场景面板填写 checkpoint 并手动换窗恢复。Full Agent 关闭底层 provider 的 emergency tool-drop 兜底。

## 记忆设计

不沿用四分类，按用途区分：

| 内容 | 用途与生命周期 |
|---|---|
| checkpoint | 当前场景/任务的交接笔记，始终进入窗口；替换式维护。应写场景、关系、用户约束、未决事项、叙事语气、下一步及必要索引 |
| 长期笔记 | 自由 key，如 `人物/艾琳`、`约定/钟楼`、`线索/信使`；默认不注入，按需搜索/读取，可更新或归档 |
| 原始历史 | 核对事实的原文，独立保存，不被摘要替换；只搜索当前分支 |

笔记 `kind` 是认识性质，非剧情分类：`fact` 有原文依据，`hypothesis` 是推测，`plan` 是计划而非已经发生的事件。fact 必须引用本回合实际搜索/读取过的 H 原文。程序回填消息身份与内容指纹，模型无需复制 UUID 或哈希。工具只能验证来源存在，依据是否语义上充分仍由模型判断。

编辑原文后，相关 checkpoint 标为待核对，笔记依据通过原文指纹显示 stale，旧工具续答退出活动窗口。不偷偷重新抽取、删除笔记或把推测变成事实。重生成恢复目标回复之前的快照；独立分支复制可见前缀的笔记、来源和工具记录，并重映射来源身份。

## 消息与数据边界

| 数据 | 正常进入上下文 | 工具召回 | 只在运行时/数据层 |
|---|---|---|---|
| 基础规则、角色、玩家、常驻设定 | 是 | 角色大字段可分页读取 | 资源身份、依赖清单、指纹 |
| 当前 checkpoint | 是 | 可主动替换 | revision、存储身份 |
| 当前窗口正文 | 是 | H 引用核对原文 | UUID、父节点、快照 |
| 当前窗口工具调用与结果 | 完整配对保留 | 可查询 T 记录 | native call ID 仅用于协议关联，不作业务参数 |
| 旧窗口正文 | 否 | 搜索、分页读取 | 全部原文持续保存 |
| 旧窗口工具记录 | 否 | 搜索、分页读取，标为过去资料 | provider reasoning 仅供传输续答，不作为可检索记忆 |
| 长期笔记 | 否，只提示数量 | 搜索、分页读取 | 原文身份、指纹与版本 |
| 动态世界书条目 | 按原锚点 system 注入 | 同一索引检索、L 引用分页读取 | 向量、索引、评分、冷却、来源身份 |
| MVU 状态 | 否 | 按路径分页读；工具更新 | schema、初始化账本、内部/显示缓存 |
| API Key、诊断、SQLite 事务、任务状态 | 否 | 不开放相关工具 | 是 |

带工具调用的 assistant 文字只进入工作记录。无工具调用且完整结束的最终回复才显示为正文。正文历史不混入 checkpoint、状态补丁和检索标签。

大结果有 token 上限和分页游标，不把截断当作完整原文。不能直接容纳的非分页结果返回 T 引用，完整内容保存供再次读取。原始工具记录位于独立 `full_agent_trace` 表，与正文在同一 SQLite 事务中提交，不依赖会被清理的诊断 events，也不在每个快照中重复复制全份工具历史。

## 检索仲裁与注入

自动检索在每轮首次模型请求前运行，可在设置中关闭。`search_lore(query)` 允许模型主动查询，不拼接旧自动检索线索。两者都调用 `RetrievalEngine.prepare`，使用相同关键词/BM25/embedding/reranker 索引、准入、递归与预算规则。

同一 Full Agent host 只有一个检索通道，包括打开故事时的索引预热。正在执行的查询结束后才允许下一项进入；等待中的主动查询优先于自动查询。不抢占已经开始的线程/模型推理；取消也等它结束才释放通道，避免假取消造成并行。索引内部各通道原有的并行融合不受影响。

中途注入保留原语义：首次可锚定开场，后续锚定当前输入；保留期间不复制或移动，随锚点退出进入冷却。手工移除与主动查询遵守同一保留/冷却状态。已注入条目可用 `read_lore` 核对，不再次注入。可选 `set_retrieval_query` 工具给下一轮自动检索保存线索，不再要求正文附带标签。

## 工具清单

| 工具 | 作用 |
|---|---|
| `search_lore` / `read_lore` | 世界书检索、注入、分页读取 |
| `search_history` / `read_history` | 当前分支正文及工具记录，H/T 引用 |
| `write_checkpoint` / `new_context` | 交接笔记、主动换窗 |
| `search_notes` / `read_note` | 搜索、读取长期笔记 |
| `write_note` / `archive_note` | 新增、修订、归档笔记 |
| `read_state` / `patch_state` | 读取语义状态、经 MVU 校验修改 |
| `read_character` / `revise_content` | 角色资料、授权设定修改 |
| `set_retrieval_query` | 保存下一轮自动检索线索 |

工具可见性与执行校验同时生效。`patch_state` 需要本轮“更新变量”；`revise_content` 遵守原设定策略；preview 策略下整份回合草稿等待审核。程序负责调用限额、引用、权限、格式与原子提交；模型负责叙事、取舍和记忆内容。

## 当前边界

- 传输使用兼容 OpenAI Chat Completions 的原生函数调用，未实现 Responses。工具进展通过 SSE 更新，最终正文当前整段送达。
- 保留脚本初始化、格式化、正文脚本处理和 MVU 校验。暂不支持 HTML 交互生成或脚本再发模型请求，这类入口会明确报错，避免引入辅助模型。
- 历史/笔记使用当前分支内文本搜索和分页原文，不另建向量记忆系统；世界书保留原多路索引。
- 工具回归与模拟模型证明协议、数据边界和事务行为，不证明任意真实模型都能恰当写笔记或控制窗口。需要真实 RP 评估叙事、漏记、检索次数、延迟与 tokens。

主要入口：`full_agent/agent.py`、`runtime.py`、`tools.py`、`context.py`、`state.py`、`lore.py`。
