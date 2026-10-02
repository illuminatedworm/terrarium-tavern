# 文档导航

文档面向使用者、角色卡作者和开发者，描述当前仓库实现。Half Agent 与 Full Agent 使用独立的模型编排和记忆机制；模式专属页面在标题或开头注明。

## 开始使用

1. 按[安装、备份与维护](guides/maintenance.md)准备环境，再按[设置指南](guides/settings.md)配置提供商和模型。
2. 选择游玩模式：Half Agent 的参数见[历史窗口与记忆](guides/history-maintenance-settings.md)，Full Agent 的操作见[使用指南](guides/full-agent.md)。
3. 导入或[编辑角色卡](guides/card-editor.md)，结合[兼容矩阵](guides/compatibility.md)检查脚本和扩展。

| 任务 | 文档 |
|---|---|
| 阅读旧剧情、编辑、重生成和创建分支 | [阅读、编辑与分支](guides/story-history.md) |
| 配置世界书检索或排查未命中 | [世界书检索](guides/retrieval.md) |
| 移除已经注入的世界书内容 | [中途注入移除](guides/lore-removal.md) |
| 理解预计输入与实际用量 | [Token、字符与字节](guides/token-counting.md) |
| 配置缓存位置与排查读取情况 | [提示缓存](guides/prompt-cache.md) |

## 理解实现

从[应用架构与职责](design/architecture.md)和[项目目录](development/structure.md)进入代码，再按改动所属机制阅读：

| 机制 | 设计文档 |
|---|---|
| Half Agent 调用顺序、失败和审核 | [回合工作流](design/agent-workflow.md) |
| Half Agent 消息编排、窗口和覆盖水位 | [上下文生命周期](design/context-lifecycle.md) |
| Half Agent 分类记忆与来源协议 | [记忆一致性](design/memory-consistency.md) |
| Full Agent 窗口、笔记与恢复 | [Full Agent 架构](design/full-agent.md) |
| 编辑草稿、连续上下文与保存 | [角色卡编辑设计](design/card-editor.md) |
| 制卡工具、引用与续接接口 | [Card Editor 工具协议](reference/card-editor-tools.md) |
| 缓存断点、传输记录和对象生命周期 | [缓存控制](design/cache-control.md) |
| 跟进间隔和目标位置的成本取舍 | [缓存成本模型](design/cache-cost.md) |
| 模型可见数据、短编号与可逆编辑 | [模型数据边界](design/model-data-boundary.md) |
| 已选文本的完整发送与容量约束 | [文本完整性](design/text-integrity.md) |

## 接口与开发维护

| 需要查阅的内容 | 文档 |
|---|---|
| 设置范围、API、修订和数据库迁移 | [设置持久化与 API](reference/settings-api.md) |
| 模型协议、请求身份、思考和缓存字段 | [模型接入参考](reference/model-api.md) |
| Full Agent 工具参数、引用和路径规则 | [工具协议](reference/full-agent-tools.md) |
| 开发运行、测试选择、实测与打包 | [验证与发行](development/validation.md) |
| 原创和第三方许可、发行未决事项 | [许可范围与发行维护](development/licensing.md)、[许可文件](../LICENSE/README.md) |

原始日志、私人剧情、数据库和凭据不进入文档。文档维护约定见[开发入口](development/structure.md#文档维护)。
