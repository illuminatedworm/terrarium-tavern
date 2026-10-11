# 项目目录与开发入口

后端位于 `src/tavern/`，前端位于 `web/`。先根据改动所属运行模式定位入口，再查对应存储、传输或兼容模块；两种游玩模式不共享高层编排。

## 后端

CLI 入口为 [__main__.py](../../src/tavern/__main__.py)。模块直接导入所属包：

| 目录 | 职责与主要入口 |
|---|---|
| `application/` | [api.py](../../src/tavern/application/api.py) 注册通用 API，[modes.py](../../src/tavern/application/modes.py) 路由模式，[service.py](../../src/tavern/application/service.py) 编排 Half Agent |
| `core/` | 数据模型、设置、提示词、模型视图、token 和缓存策略 |
| `character/` | 角色卡格式、资源、编辑 Agent、工作区及其 API |
| `conversation/` | Half Agent 上下文、窗口、场景、工作记录与会话断点 |
| `tracker/` | Half Agent 前后置维护任务、原生工具循环和变量草稿 |
| `memories/` | 分类记忆、来源协议、投影、准出与历史补查 |
| `full_agent/` | Full Agent 模型循环、窗口、笔记、工具、检索仲裁与失败恢复 |
| `search/` | 召回、准入、文本解析、本地模型进程和索引 |
| `storage/` | SQLite、事务、分支、备份与存储清理 |
| `scripting/` | 共享模型传输、原生回复、隔离脚本、依赖和 MVU 处理 |

`skills/`、`tokenizer_data/` 是随包资源，`static/` 保存生成的界面与脚本。角色卡适配使用 [内置 skill](../../src/tavern/skills/adapt-card-to-tavern/SKILL.md)，不依赖开发机器的个人配置。

## 前端与生成资源

[App.vue](../../web/App.vue) 和 [main.ts](../../web/main.ts) 组装应用。`components/` 按 `common`、`story`、`settings`、`character`、`records` 分类；`lib/` 保存请求与业务辅助逻辑，`composables/` 保存组合式函数。

两种对话流分别由 [HalfAgentDialogue.vue](../../web/components/story/HalfAgentDialogue.vue) 与 [FullAgentDialogue.vue](../../web/components/story/FullAgentDialogue.vue) 负责，正文、编辑、分支和版本操作共用 [DialogueMessage.vue](../../web/components/story/DialogueMessage.vue)。

[StoryHistory.vue](../../web/components/story/StoryHistory.vue) 与 [storyHistory.ts](../../web/lib/storyHistory.ts) 管理动态高度虚拟列表；[ConversationRail.vue](../../web/components/story/ConversationRail.vue) 管理历史刻度导航。脚本交互实例保留到退出会话视图，避免因虚拟列表滚动而重复运行或丢失草稿。界面渲染范围不影响模型窗口。

引号字体源产物为 [quote-marks.ttf](../../web/assets/quote-marks.ttf)，只覆盖中文单引号 U+2018/U+2019 和双引号 U+201C/U+201D，以一枚或两枚弯曲引号区分，不覆盖角括号。使用 [build-quote-font.py](../../tools/build-quote-font.py) 与 fonttools 4.61.1 重新生成；fonttools 不属于应用运行依赖。用户可见行为见[历史阅读](../guides/story-history.md#世界书与文字显示)。

`compat/` 是兼容桥，`vendor/` 保存固定第三方源码。前端构建经 [build-mvu.mjs](../../tools/build-mvu.mjs) 和 [package-web.mjs](../../tools/package-web.mjs) 生成后端静态资源；修改源文件后重新构建，避免直接维护生成文件。

## 常见改动路径

| 改动 | 先读 | 同时检查 |
|---|---|---|
| Half Agent 调用顺序或失败处理 | [工作流](../design/agent-workflow.md) | 事务、回调顺序、来源覆盖与取消 |
| Full Agent 工具或换窗 | [架构](../design/full-agent.md)、[工具协议](../reference/full-agent-tools.md) | 强制换窗、进度恢复和工具权限 |
| 模型协议或缓存 | [接入参考](../reference/model-api.md)、[缓存设计](../design/cache-control.md) | 最终 HTTP 序列化、原生回复和 usage |
| 设置字段 | [设置 API](../reference/settings-api.md) | 默认值、迁移、范围修订和前端保存合并 |
| 卡片编辑 | [工作区设计](../design/card-editor.md) | 草稿版本、资源引用和模型视图恢复 |
| 检索或上下文 | [检索](../guides/retrieval.md)、[上下文](../design/context-lifecycle.md) | 准入、窗口计数和预览一致性 |

测试集中在 `tests/`，命令及选择方式见[验证与发行](validation.md)。

## 文档维护

文档按读者任务分层：`guides/` 说明配置、操作和排查；`design/` 说明当前职责、约束与取舍；`reference/` 保存工具和 API 契约；`development/` 说明开发和发行。

维护时将默认值和协议定义集中在对应参考页或设置表，其他页面通过内部链接引用。模式专属内容在开头注明；迁移使用源／目标格式版本，历史调查注明归档基线。代码入口使用仓库内完整路径，新增页面加入[主导航](../README.md)。常驻文档使用当前规则，任务交付过程留在提交记录中。

## Git 与发行边界

`data/`、备份、恢复目录、`.cache/`、日志、数据库、依赖环境及构建产物不提交。锁文件、合成夹具、示例卡、tokenizer 词表和第三方许可属于必要源文件。

环境变量文件和密钥被忽略，仅保留无真实值的 `.env.example`／`.env.template`。自定义数据或模型目录优先放在仓库外；位于仓库内时先添加忽略规则。提交前检查 `git status --short`、暂存差异和文件内容，避免用强制添加绕过保护。

`.gitignore` 不会清除已跟踪文件，也不能识别任意私人正文。已泄露密钥需要撤销，并处理历史记录。发行包附带的许可范围与检查项见[许可与发行](licensing.md)。
