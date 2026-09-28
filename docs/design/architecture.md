# 架构与一致性

应用有 Half Agent 与 Full Agent 两种独立游玩模式。本文的 Writer / Tracker / Roleplay Runtime 对应 **Half Agent**。Full Agent 通过 `application/modes.py` 路由到独立引擎，只共享底层检索、存储与卡片 IO，不调用前后台编排；详见 [Full Agent 架构](full-agent.md)。

Vue → FastAPI → Roleplay Runtime → Writer / Tracker / 隔离 Chromium → SQLite。

## 正式角色与职责

| 角色 | 职责 | 边界与代码入口 |
|---|---|---|
| 前台 Writer | 演绎角色、生成正文，输出约定的检索线索；变量由独立后台分析更新 | `agent.py: Writer`；每回合一次模型调用，零工具、零子 Agent，不调度 Tracker、不提交状态 |
| 后台 Tracker | 整理分支记忆、保留事实来源；独立 MVU 更新；按授权执行设定维护 | `memory.py: Tracker`、`memory_engine.py`；记忆模式仅开放只读历史补查，整次操作最多3次；设定维护仍最多四次模型调用，并共享历史工具额度 |
| Card Editor | 独立角色卡编辑与工作流适配；不读取剧情 | `card_editor.py`，最多 12 次调用，草稿工具，用户审阅后保存；见[编辑指南](../guides/card-editor.md) |
| Roleplay Runtime | 管理对话与回合，调度 Writer/Tracker，检索并注入世界书，组装提示词，管理窗口和预算，执行脚本、审核与事务提交 | `service.py: RoleplayRuntime` 为入口，协同 `context.py`、`retrieval.py`、`window.py`、`store.py` 等；是程序而非第三个模型 Agent |

Writer 与 Tracker 通过 Runtime 管理的回合草稿交换数据，不互相委派。Tracker 不是常驻自主循环；普通发送需要记忆整理时先等待其完成，再生成正文。
`agent.py: TurnAgent` 是两种模型模式共用的底层适配器，不是第四个角色。`runtime.py: ScriptRuntime` 仅执行隔离卡片脚本，不等同于 Roleplay Runtime。

API/配置字段 `foreground` 和 `background` 分别代表 Writer 和 Tracker；可选 `card_editor` 配置为空时继承后台模型；事件用途 `narrative`、`memory`、`maintenance` 用于区分调用目的。界面的“后台工作记录”汇集整个 Runtime 的工作事件，不专指 Tracker。

## 数据与事务

消息有父节点，快照保存状态、记忆、世界书注入、脚本变量和设定草稿。
只有完成所有正文处理和回调后才提交。消息、快照、会话 head/revision 与提交事件共用事务。
operation_id 保证会话内幂等，expected_revision 检查乐观并发，运行和待审核回合互斥。
SSE 只是观察者；断开不取消生成。前端按事件序号去重，切换会话使旧请求失效。

书架角色卡以 revision 校验保存。每个对话在创建时保存自有角色资料与依赖；库内保存和删除不改变旧对话。独立编辑草稿也保护其资源引用。
分支复制消息前缀及回合快照，重映射来源和锚点；源对话删除不影响新分支。
编辑原文保留记忆并标注来源已修改，失效检索线索和窗口；只有手动重建才重新核对旧记忆，也不自动重演后续变量。修订变量应使用变量界面或从较早进度重生成。

数据库采用 schema 2；打开前先检查格式。schema 1 在备份后迁移独立设置，其他不支持的版本拒绝打开且不执行 DDL。持久设置按 full_agent、half_agent、shared 分区，运行时配置仅为操作快照；详见[设置说明](../guides/settings.md)。
完整快照仍是恢复策略；剧情分支随用户保存数量增长，不被日志清理当成旧版本删除。

## 上下文职责

检索器产生候选、准入和递归结果。窗口规划与编译使用同一份候选清单。
窗口条数只计剧情与当前输入，中途世界书占用token容量并随交流退出；Runtime 先调度 Tracker 完成真实剧情来源覆盖。
正文编译不隐式扩展候选，最终 HTTP 边界再次核算消息和工具开销。
上下文使用本地 BPE 分词器估算 tokens；服务商实际 tokens 单独展示。

Writer 每回合新建无工具 Creature，关闭基座 compact；Tracker 的授权维护模式另建有工具 Creature，最多四次调用。
工具可见性和执行权限同时限制，上游通用管理工具不可用。
OpenAIProvider 的实例 HTTP client 用于代理和最终请求检查；固定上游提交并用请求序列化测试保护升级。

模型业务输入遵循[数据边界](model-data-boundary.md)：机械身份和哈希由程序处理，必要引用使用短编号。`model_view.py` 负责授权维护/Card Editor 的可逆视图，以及剔除状态中的初始化账本、schema、重复展示缓存与内部元数据；原始快照和 MVU 校验数据保持完整。

## 隔离与维护

角色 HTML 运行在无 allow-same-origin 的 sandbox iframe，桥检查来源窗口和随机通道。
服务端脚本 realm 阻止网络，不接触 API 密钥；每回合重新建立，持久数据须通过变量 API 保存。
外部依赖先下载、哈希缓存和离线打包，运行阶段不任意联网。

服务只监听 loopback，校验 Host/Origin，不是多人公网服务。
密钥保存在后端本地配置，设置响应不返回密钥；备份也应按私有数据保管。
详细日志默认 14 天/512 MiB，当前和可恢复回合受保护；索引默认 256 MiB，可重建。
窗口、日志、索引和模型权重是不同生命周期，不能用同一清理规则。

## 分类记忆

周期、裁剪前与手动整理共用来源校验及受限复核引擎；分类条目是新的内容来源，Writer只接收其自然语言投影。记忆预算与后台MVU输出预算分开；手工条目及删除标记不会被自动覆盖。详见[记忆设计](memory-consistency.md)。

`memory_protocol.py` 将模型请求与存储记录隔离：剧情片段用 S/H、已有记忆用 R、参考资料用 E 的请求内短编号。模型只选编号，Runtime 回填真实消息ID、原文摘录、指纹及设定版本，并生成新增条目的永久ID。每批与复核独立建表，历史工具只登记实际返回片段；所有恢复后的引用仍经过原来的分支与来源校验，不增加请求、不改变快照格式。
