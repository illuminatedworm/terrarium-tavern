# 应用架构与职责

应用由两种游玩模式、参谋 agent 和制卡 agent 组成，共享模型传输、检索和存储能力。各运行时分别管理工作上下文与写入范围，设计说明见 [Full Agent](full-agent.md)、[参谋 agent](advisor.md)和[制卡 agent](card-author.md)。

```mermaid
flowchart TD
    UI[Vue 界面] --> API[FastAPI 与模式路由]
    API --> HALF[Roleplay Runtime · Half Agent]
    API --> FULL[Full Agent Runtime]
    API --> EDIT[制卡 agent]
    API --> ADVISOR[参谋 agent]
    HALF --> WRITER[Writer 正文]
    HALF --> TRACKER[Tracker 维护]
    HALF --> SHARED[共享存储、检索、传输与脚本能力]
    FULL --> SHARED
    EDIT --> SHARED
    ADVISOR --> SHARED
```

## 正式角色与职责

| 角色 | 职责 | 代码入口与边界 |
|---|---|---|
| Writer | 演绎角色、生成正文及检索线索 | [scripting/agent.py](../../src/tavern/scripting/agent.py) 的 `Writer`；正文一次生成，无业务工具；可选实验层声明仅传输正文的函数，不调度 Tracker 或提交状态 |
| Tracker | 分类记忆、授权对话设定和 MVU 变量维护 | [tracker/workflow.py](../../src/tavern/tracker/workflow.py) 调度，[tracker/task.py](../../src/tavern/tracker/task.py) 执行原生工具循环；工具按本轮任务授权 |
| 制卡 agent | 独立编辑和适配已引用卡片的草稿 | [character/card_author.py](../../src/tavern/character/card_author.py)；用户审阅后保存书架卡片 |
| 参谋 agent | 查阅故事、咨询与联网；按明确要求更新本轮偏好 | [advisor/runtime.py](../../src/tavern/advisor/runtime.py)；使用独立咨询记录和偏好草稿 |
| Full Agent | 通过工具检索、维护笔记并生成正文 | [full_agent/agent.py](../../src/tavern/full_agent/agent.py)；独立模型循环 |
| Roleplay Runtime | Half Agent 的回合、检索、上下文、窗口、脚本、审核和提交 | [application/service.py](../../src/tavern/application/service.py) 的 `RoleplayRuntime`；程序编排者 |
| 模式路由 | 将故事操作交给对应引擎 | [application/modes.py](../../src/tavern/application/modes.py)；Full Agent 不调用 Writer／Tracker 编排 |

Writer 和 Tracker 的调用及草稿交换由 Runtime 统一编排。前置任务处理必要的裁剪补整理及明确设定修改，后置任务合并到期记忆与授权变量维护。调用限额见[Half Agent 设置](../guides/history-maintenance-settings.md#记忆与任务限额)。

`scripting/agent.py` 的 `TurnAgent` 适配 Writer 与 Terrarium；[scripting/runtime.py](../../src/tavern/scripting/runtime.py) 的 `ScriptRuntime` 执行隔离卡片脚本；`RoleplayRuntime` 负责整个回合的调度和提交。

## 数据所有权与事务

SQLite 是剧情和状态的权威来源。消息通过父节点构成历史，快照保存状态、记忆、世界书注入、脚本变量与对话设定草稿。完成正文处理与回调后，消息、快照、会话 head/revision 和提交事件在同一事务中生效。

| 机制 | 约束 |
|---|---|
| `operation_id` | 会话内幂等；相同操作及内容返回原结果，不重复执行 |
| `expected_revision` | 乐观并发检查，拒绝覆盖已变化的会话 |
| 回合互斥 | 一个会话只允许一个运行或待审核回合 |
| SSE | 观察执行，不控制生命周期；断开不取消生成 |
| 前端请求归属 | 按事件序号去重，切换会话后忽略旧请求响应 |

书架角色卡保存使用 revision。创建故事时复制角色资料、脚本权限和依赖清单，书架后续保存或删除不改变旧故事。书库世界书同样按副本挂载：条目复制进对话级角色卡，来源记录保存在快照中，书库后续修改只在用户重新载入时进入对话，见[世界书库与挂载](../guides/worldbooks.md)。编辑草稿和副本共同保护引用资源。

独立分支复制可见消息前缀和快照，重映射记忆来源及世界书锚点；删除源会话不影响分支。编辑或同步对话设定、卡片交互把最新回复复制为同一轮的新版本，以同一套规则重映射该回复的引用、覆盖指纹和请求轨迹，见 [message_copy.py](../../src/tavern/storage/message_copy.py)。操作语义见[工作流](agent-workflow.md#操作边界)。

## 设置与恢复

设置文档按 `full_agent`、`half_agent` 和 `shared` 持久化，运行时使用一次操作的配置快照。Writer、Tracker、Full Agent、参谋 agent 和制卡 agent 各有独立模型配置。“后台工作记录”按回合汇集 Runtime 事件。

数据库格式版本、升级前备份与迁移规则见[设置 API](../reference/settings-api.md#数据库与设置迁移)；不支持的版本在写入前报错。

剧情快照持续保留，恢复和回复版本切换使用对应快照。详细诊断可以清理，但剧情、分支及恢复所需记录不作为旧日志删除。存储策略见[安装与维护](../guides/maintenance.md#清理与资源保留)。

## 上下文与模型传输

检索器输出最终候选、准入证据和递归结果。Half Agent 窗口规划与编译消费同一清单，编译器不自行增加候选。中途世界书占 token 容量，不占剧情条数；退出原文前由 Tracker 校验整理覆盖。

Writer 每回合建立无工具 Creature，关闭基座自动 compact；Tracker 使用独立工具上下文。通用管理工具不进入模型白名单。最终 HTTP 请求统一检查协议和内容占用，本地超量估算遵循[文本完整性](text-integrity.md)。

模型业务输入使用[数据边界](model-data-boundary.md)定义的自然语言投影和短引用。持久身份、指纹、schema 与初始化账本留在运行时；原始快照仍保存完整数据。

两种模式共用 [模型传输](../reference/model-api.md)，分别管理原生回复和动态上下文记录。固定依赖版本和最终请求序列化测试共同保护升级。

## 脚本与资源隔离

角色 HTML 运行于不含 `allow-same-origin` 的 sandbox iframe，桥接检查来源窗口和随机通道。服务端脚本 realm 阻止网络，不接触 API 密钥，每回合重新建立；持久数据必须通过变量 API 保存。

`ScriptRuntime` 与制卡 agent 的本地验证只通过 [realm.py](../../src/tavern/scripting/realm.py) 定义的后端接口操作隔离页面，接口说明列出各后端必须保持的隔离与加载约定；桌面后端为 [playwright_realm.py](../../src/tavern/scripting/playwright_realm.py) 的 Playwright Chromium。Android 使用 [android_realm.py](../../src/tavern/scripting/android_realm.py)，通过管道与独立的 WebView 进程通信：故事脚本使用 `:realm`，制卡验证与依赖打包使用 `:realm_tools`。页面拥有独立来源，网络请求只能由资源回调提供内容，不能访问主界面的 cookie、启动令牌或原生能力。脚本取消或超时时重置所在进程，该进程内的环境随之失效，新环境可重新启动；另一类进程不受影响。

卡片的 ES 模块依赖先下载并固定内容，再由隔离页面中的 esbuild-wasm 打包。打包器只读取清单内的模块，并保留 lodash、jquery、zod、yaml 到兼容 SDK 全局对象的映射；缺失模块直接报错，不在打包期间联网。`npm run build:sdk` 同时打包脚本工具及其许可，应用运行时不需要 Node。

外部依赖先下载、哈希缓存和离线打包。卡级缓存及已落盘的旧会话依赖可以复用，解析后回填卡级清单；每个故事仍有自己的依赖清单。运行阶段不任意联网，兼容范围见[矩阵](../guides/compatibility.md)。

服务只监听 loopback，并校验 Host/Origin。API 密钥保存在后端配置，设置响应只提供配置状态；完整备份包含这些密钥，需按私有数据保存。ChatGPT 凭据单独绑定当前安装，迁移后重新连接。窗口、日志、索引和模型权重有不同生命周期。

Android 宿主每次启动生成随机令牌，通过 `TAVERN_STARTUP_TOKEN` 传入后端；API 与静态资源都进行常量时间认证。主 WebView 使用 `tavern_session` 的 HttpOnly、SameSite=Strict cookie，原生请求可使用 `X-Tavern-Token` 请求头。令牌模式同时固定实际监听端口的 Host，并拒绝开发服务器的跨来源例外；健康接口只返回平台能力，不回传令牌。桌面未设置该环境变量时保持原本的本地访问边界。
