# 项目目录与提交边界

## 后端

`src/tavern/__main__.py` 是 CLI 入口；模块按职责分组，直接导入所属包。

| 目录 | 职责 |
|---|---|
| `application/` | FastAPI 接口和 Roleplay Runtime 编排 |
| `core/` | 数据模型、默认提示词、模型数据视图与 token 计数 |
| `character/` | 角色卡格式、编辑 Agent、草稿及资源引用 |
| `conversation/` | 上下文、窗口、场景与工作记录 |
| `memories/` | Tracker、分类记忆、来源协议及历史补查 |
| `search/` | 检索、准入、文本解析与本地模型 |
| `storage/` | SQLite、备份恢复和存储清理 |
| `scripting/` | 模型调用适配、隔离脚本、外部依赖及变量分析 |

`skills/`、`tokenizer_data/` 为随包资源；`static/` 保存构建生成的界面与脚本。

## 前端、测试和文档

`web/App.vue` 和 `web/main.ts` 负责应用组装。`components/` 按 `common`、`story`、`settings`、`character`、`records` 分类；`lib/` 为请求与业务辅助逻辑，`composables/` 为共享组合式函数，`styles/` 和 `assets/` 保存样式与素材。

`compat/` 为兼容桥，`vendor/` 为固定上游源码。测试统一位于 `tests/`，具体入口见[验证说明](validation.md)。文档分为 `guides/` 使用指南、`design/` 当前实现说明和 `development/` 开发指引；不保存历史验收报告。

[`LICENSE/`](../../LICENSE/README.md) 集中保存本项目非商业许可、Kohaku Terrarium 上游许可原文和第三方声明；`vendor/` 内原有第三方许可保留在源码旁。前端构建、Python 包和源码发行包均随附许可文件。

## Git 与发行隐私

`data/`、备份与恢复目录、`.cache/`、日志、数据库、依赖环境及构建产物不提交。锁文件、合成测试夹具、示例卡、内置 tokenizer 词表和第三方许可必须保留。

环境变量文件和密钥被忽略；仅允许不含真实值的 `.env.example` / `.env.template`。自定义数据和模型目录优先放到仓库外；放在仓库内时须先添加忽略规则。不要使用 `git add -f` 绕过保护。

推送前检查 `git status --short`、`git diff --cached --stat` 和暂存内容。`.gitignore` 不能清除已跟踪文件或识别正文中的任意私人信息；若曾提交密钥，应撤销密钥并处理历史记录。
