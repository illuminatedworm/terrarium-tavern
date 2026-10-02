# 角色卡编辑工作区设计

Card Editor 是独立于游玩模式的草稿编辑 Agent。它读取角色卡及编辑交互，不读取剧情、分类记忆或当前游玩变量。用户操作见[角色卡编辑指南](../guides/card-editor.md)。

## 数据所有权

| 对象 | 内容与生命周期 |
|---|---|
| 书架角色卡 | 已保存原文、revision、脚本权限和资源引用 |
| 编辑工作区 | 一份独立草稿、基准卡片版本、聊天、撤销历史与保存操作记录 |
| 编辑任务 | 单轮模型请求、工具动作、临时修改、状态和事件 |
| 编辑上下文 | 已完成请求前缀、原生回复与可逆短编号映射 |

同一张卡可以有多个工作区。新建对话从最新已保存卡片创建；沿用草稿则复制所选工作区上一次完成的草稿和基准版本，建立空聊天。运行中的临时修改不参与复制。切换工作区只改变观察对象，不取消旧任务。

覆盖原卡使用卡片 revision 校验。另一个工作区已保存原卡时，旧基准覆盖会冲突；草稿仍可另存。库内保存、复制或删除不修改已有故事的角色资料。

## 单轮执行与提交

1. 从已完成草稿或用户当前表单建立本轮起点，记录用户编辑要求。
2. 构造卡片概览和最近完成的编辑交互，开放受限工具。
3. 模型读取完整字段或搜索条目，在临时文档上提交 JSON Patch，并执行静态验证。
4. 单轮最多 12 次模型调用，最后一次仅生成说明。
5. 成功时原子保存完整草稿、请求记录和编号映射。失败、取消或服务重启时保留上一个完成版本。

模型不能直接保存书架卡片。它可以读取概览、完整字段、条目、内置 skill，修改草稿、迁移开场并验证结构；没有 shell、任意文件或网络浏览工具。用户审阅差异后执行覆盖或另存。

SSE 是任务观察通道，断线不取消执行。前端恢复已完成交互和动作事件，按当前工作区身份隔离迟到响应；每个工作区保留自己的未发送输入。

## 模型视图与连续上下文

卡片经 `ModelView` 投影后再分页。长身份和不透明路径键映射为 @D 编号，指纹与哈希隐藏；应用 JSON Patch 前恢复真实键和身份。替换父对象、数组重排或插入时保留正确的隐藏字段，未知编号和隐藏字段修改被拒绝。详细约束见[模型数据边界](model-data-boundary.md)。

每个编辑对话有稳定的会话 UUID，每轮有独立请求操作身份。Responses 路由状态仅在该轮工具续答中复用；Anthropic 原生内容块与 Responses 输出保留顺序、工具 ID、签名和加密推理。

已发送内容保持顺序，工具结果和变化后的草稿概览追加到末尾。未变化的概览不重发。已完成上下文与草稿一起持久保存，刷新、重启、诊断日志清理及备份恢复均保留它。

以下变化使原记录不再适合续答，需要重建基线：模型、提供商或认证改变；固定指令改变；最近交互窗口改变；手工草稿改变；新输入中的字面短编号与旧映射冲突。旧记录缺失时，从可用聊天和草稿建立基线。新建对话不复制旧上下文，删除工作区时同步删除。缓存标记只覆盖固定系统前缀，命中情况见[缓存设计](cache-control.md)。

## 工作流适配

“适配本项目工作流”使用随包提供的 [adapt-card-to-tavern](../../src/tavern/skills/adapt-card-to-tavern/SKILL.md)。普通润色不自动触发适配。适配目标是区分叙事设定、Tracker 的变量规则和确定性初始化；用途字段见[角色卡编辑指南](../guides/card-editor.md#世界书用途与初始化)。

混合叙事和变量规则的世界书条目需要拆分：原叙事保留原 ID，新规则记录 `split_from`；未知扩展、schema 和校验回调保留。无法可靠处理的动态脚本保持原状并列入人工检查项。

默认适配不修改开场、内联 MVU 初始化／更新块或配套的提示词过滤和状态栏正则，也不要求创建 `extensions.terrarium.workflow`。用户明确要求迁移开场时才使用 `set_opening`，由程序维护稳定身份和正文指纹。

显式迁移只接受 InitVar YAML、JSON Patch 和受限 MVU 数据指令，不执行模型生成的任意 JavaScript。世界书提供默认值，所选开场 InitVar 值覆盖它，再执行独立开场更新。静态验证检查结构和分流规则，不证明动态脚本的语义等价。

## API、保存与删除

| 入口 | 用途 |
|---|---|
| `POST /api/cards/{cid}/copy` | 复制已保存卡片 |
| `/api/cards/{cid}/edits`、`/api/card-edits/{id}` | 创建、恢复、更新、撤销和保存工作区 |
| `POST /api/card-edits/{id}/new-context` | 通过 `expected_revision` 校验，从已完成草稿创建空上下文 |
| `/api/card-edit-runs/{id}` | 观察编辑任务 |
| `/api/card-edit-runs/{id}/activity?after=序号` | 分页读取动作记录 |
| `/api/card-edit-runs/{id}/events` | SSE 观察与重连 |

卡片更新使用 `expected_revision`。保存指定 `overwrite/copy` 及 `operation_id`，防止重试重复创建副本。先保存再删除工作区的操作还会核对草稿版本；保存失败、原卡冲突或草稿再次改变时不继续删除。

工作区、聊天、撤销历史与资源引用随 SQLite 备份。运行中的任务阻止备份和清理；未保存草稿不自动清除。已保存工作区的详细诊断事件按日志保留策略清理，但完成的编辑上下文独立保存。删除原卡后，草稿可继续另存副本。只有失去全部引用的资源才可回收。

## 前端与验证入口

[CardAgentEditor.vue](../../web/components/character/CardAgentEditor.vue) 展示编辑聊天、角色数据树、诊断和差异。动作记录包括开始、完成与失败，预览最多 4000 字符，不将完整模型请求加入动作列表。数据树按需渲染，运行中显示上次完成的草稿。

后端入口为 [card_editor.py](../../src/tavern/character/card_editor.py)、[card_editor_store.py](../../src/tavern/character/card_editor_store.py)、[card_editor_context.py](../../src/tavern/character/card_editor_context.py) 与 [card_editor_api.py](../../src/tavern/character/card_editor_api.py)。重点回归为草稿原子性、并发保存、短编号恢复、工作区隔离和资源引用，见 [test_card_editor.py](../../tests/backend/test_card_editor.py)、[test_card_editor_alignment.py](../../tests/backend/test_card_editor_alignment.py)。
