# Card Editor 独立创作工作区

Card Editor 有独立对话、资料引用和草稿，不读取游玩会话、记忆或变量。操作见[使用指南](../guides/card-editor.md)，工具详见[协议](../reference/card-editor-tools.md)。

用户明确要求时还可通过四个用户角色工具列出、读取、新增和修改 persona 名称及介绍。普通制卡不自动加载用户角色库。工具声明和系统策略限定明确意图，request_ref 必须指向本任务最新用户消息；助手提议和旧授权不能替代当前要求。

## 数据所有权

schema 3 的 `card_editor_conversations` 保存会话版本、标题、单调引用计数及完整卡片集合/创作状态。每个卡片对象保存 C 引用、当前文档、原卡快照、基准版本、目标模式与确认方案原文。编辑引用时保持编号，变体另分配编号，丢弃的编号不复用。

`card_editor_messages` 保存完整用户输入与最终回复，以会话内 H 编号引用；`card_editor_traces` 保存完整工具交换。`card_edit_contexts` 保存当前请求前缀、可逆编号映射和窗口状态。它们独立于可清理的动作诊断。

`card_editor_progress` 保存未提交卡片集合、轮前快照、待执行调用及结果、读取记录、完整上下文和换窗进度。每个调用的修改与结果一起持久化；正式草稿只在完整最终回复和结构校验之后共同提交。

用户角色 P 引用到存储身份的映射保存在会话 state，读版本和待保存修改保存在 progress。未授权任务不自动查询 persona 表。用户角色新增/修改与编辑任务完成在同一事务中提交，复用 Store.save_persona 的版本检查；卡片仍仅提交独立草稿。中断不会产生部分用户角色写入，续接保留读版本与草稿，换窗后修改须重新读取。用户撤销时仅清除待保存修改，原 persona 与已有故事快照不受影响。

## 执行与恢复

服务层使用项目原生工具循环和共用模型传输。完整响应先落盘，再顺序执行工具并保存配对结果。原要求续接跳过已配对调用；补充要求先给未执行调用补取消结果，再追加用户消息，避免执行旧意图。

停止、传输失败、重启或额度耗尽保留进度。用户显式继续开启新的有界批次，默认 24 次模型请求，换窗不重置。丢弃进度不修改完成版本。运行或待续进度期间拒绝人工草稿修改、保存和引用刷新，使用会话 revision 校验。

单卡保存另检查其原卡 base_revision，成功后更新该卡基准，不更换 C 引用。覆盖、首次创建和另存分别处理；保存 operation_id 幂等。撤销新记录同时恢复文档与保存基准，避免撤销重新载入后获得覆盖新版原卡的权限。历史旧撤销记录保留可用的原文。

## 请求编译与工具边界

系统说明和严格 schema 由固定目录维护，动态卡片概览、确认方案、handoff 只在变化时追加。工具说明、只读预览和模型请求使用同一注册表。读取结果完整投影后发送；日志摘要长度不影响模型正文。

模型用 C 编号定位卡片，H 编号读取编辑历史，用 @D 处理文档内部不透明身份。ModelView 恢复路径和身份、保留隐藏字段；数据库 ID、指纹和密钥不作为模型业务字段。已有读取覆盖修改子树；旧单卡接口为兼容既有调用可补唯一卡片编号。

新卡先讨论方案再制作，或接受明确的直接制作要求。create_draft 校验方案/确认引用的会话、角色和先后关系，保存原文；确认的语义由 Agent 根据真实用户消息判断。目标模式单独记录，普通文本卡不强制引入项目扩展。

## 长对话

不按固定运行记录数量删旧上下文。到配置的 70% 比例提醒，达到窗口上限只接受单独 new_context；混合换窗批次不执行。token 估算包括工具定义和原生续答。

换窗保存 handoff，保留确认方案、卡片目录、当前任务输入和最近两轮完整用户/最终回复，旧工具交换一起退出。原文仍可通过 history 工具读取。模型或协议变化重建兼容上下文；恢复时维持卡片和文档引用含义。单个已选原文不截断、分页或摘要替代。

## 本地沙盒

`test_draft` 复用 ScriptRuntime，用临时状态启动一个指定开场，检查初始化、脚本启动及正则样例；浏览器上下文禁止网络，宿主只接受本地日志/事件，不开放额外模型调用。不解析下载新依赖，不修改故事、脚本权限或卡片。缺失模块、超时、执行错误和未测项目分别报告。正则转换只报告 observed（无规则则 skipped），前端通过 display_assertions 验证真实 DOM；可选 state_patch 经 Runtime 应用到临时快照，再按右侧 MVU 区重建页面的方式验证 after_text。测试不从补丁日志推导 HTML，不把初始化成功报告为前端成功。

两种模式共用 writer 叙事、tracker_mvu 变量规则与 runtime_init 初始化的分流及静态检查。普通制卡和 MVU 设计不要求目标模式；可选 target_mode 只记录用户的兼容性目标，特殊脚本能力影响方案时才需明确。共用 runtime-guide.md 与适配资料解释执行差异，不为 Full Agent 另设规则迁移流程。普通开场更新保留已有初始化，只有显式初始化参数才进行独立关联操作。

## API 与迁移

主入口 `/api/card-editor/conversations` 提供列表、创建、读取、改名与删除；其下 references、drafts、runs、progress、tools、preview 分别处理引用、逐卡操作、执行、续接进度、工具目录与预览。运行观察继续使用 `/api/card-edit-runs/{id}` 的 activity/events/cancel 接口。

旧 `/api/cards/{cid}/edits` 和 `/api/card-edits/{id}` 作为单卡适配层；多卡操作必须明确目标。旧 new-context 只复制完成的单卡草稿，不复制旧聊天或未提交修改。

打开 schema 1/2 数据库先只读验证并创建 `before-card-editor-v3-*.sqlite3` 备份。编辑迁移在事务内保留旧会话 ID、卡片基准、聊天、撤销与资源，最后写入 schema 3。旧程序因此在写入前拒绝打开新格式。备份包含进度；清理保护未保存及待续数据，资源回收考虑引用、草稿、撤销快照和正式故事。

界面：[CardWorkspace.vue](../../web/components/character/CardWorkspace.vue)。运行和上下文：[editor_run.py](../../src/tavern/character/editor_run.py)、[editor_context.py](../../src/tavern/character/editor_context.py)。回归：[test_card_authoring.py](../../tests/backend/test_card_authoring.py)、[test_card_editor_alignment.py](../../tests/backend/test_card_editor_alignment.py)、[浏览器验收](../../tests/browser/browser_card_authoring.py)。真实模型评测显式运行 tools/evaluate_card_editor.py，使用隔离数据与累计 60 请求计数，报告仅保存到忽略目录。
