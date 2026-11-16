# 文档导航

按安装、使用、角色卡制作和开发任务查阅。游玩、制卡和咨询分别使用自己的模型与上下文。

当前版本：**v0.8.16（实验阶段）**。[GitHub Release](https://github.com/illuminatedworm/terrarium-tavern/releases/tag/v0.8.16) 提供 Windows x64 便携包和 Android ARM64 APK。历史里程碑、版本变化与升级注意事项见[版本记录](changelog.md)。

## 开始使用

1. 按[安装、备份与维护](guides/maintenance.md)准备环境，再按[设置指南](guides/settings.md)配置提供商和模型。
2. 从 [游玩指南](guides/story.md) 了解游玩、工具权限和工作窗口。
3. 导入或[编辑角色卡](guides/card-author.md)，结合[兼容矩阵](guides/compatibility.md)检查脚本和扩展。

| 任务 | 文档 |
|---|---|
| 阅读旧剧情、编辑、重生成和创建分支 | [阅读、编辑与分支](guides/story-history.md) |
| 咨询剧情、查资料、调整参谋 agent 模型和上下文 | [参谋 agent 聊天](guides/advisor.md) |
| 配置世界书检索或排查未命中 | [世界书检索](guides/retrieval.md) |
| 管理独立世界书、挂载到对话和设置新建对话默认项 | [世界书库与挂载](guides/worldbooks.md) |
| 移除已经注入的世界书内容 | [中途注入移除](guides/lore-removal.md) |
| 理解预计输入与实际用量 | [Token、字符与字节](guides/token-counting.md) |
| 配置缓存位置与排查读取情况 | [提示缓存](guides/prompt-cache.md) |
| 调整应用消息的角色与标签 | [消息类型与适配](guides/message-adapter.md) |

## 理解实现

从[应用架构与职责](design/architecture.md)和[项目目录](development/structure.md)进入代码，再按改动所属机制阅读：

| 机制 | 设计文档 |
|---|---|
| 参考文件、联网调查与报告送达 | [资料调查](design/novel-research.md) |
| 剧情窗口、笔记与恢复 | [剧情架构](design/story.md) |
| 独立参谋 agent 上下文、工具与偏好提交 | [参谋 agent 设计](design/advisor.md) |
| 编辑草稿、连续上下文与保存 | [制卡 agent 设计](design/card-author.md) |
| 制卡工具、引用与续接接口 | [制卡 agent 工具协议](reference/card-author-tools.md) |
| 外部搜索限量、网页分页与来源完整性 | [联网工具协议](reference/web-tools.md) |
| 缓存断点、传输记录和对象生命周期 | [缓存控制](design/cache-control.md) |
| 跟进间隔和目标位置的成本取舍 | [缓存成本模型](design/cache-cost.md) |
| 模型可见数据、短编号与可逆编辑 | [模型数据边界](design/model-data-boundary.md) |
| 内部文本完整发送与外部资料限量 | [文本完整性](design/text-integrity.md) |

## 接口与开发维护

| 需要查阅的内容 | 文档 |
|---|---|
| 设置范围、API、修订和数据库迁移 | [设置持久化与 API](reference/settings-api.md) |
| 模型协议、请求身份、思考和缓存字段 | [模型接入参考](reference/model-api.md) |
| 剧情模型全部工具的简短列表与作用 | [工具速查](reference/story-tools-summary.md) |
| 剧情工具参数、引用和路径规则 | [工具协议](reference/story-tools.md) |
| 开发运行、测试选择、实测与打包 | [验证与发行](development/validation.md) |
| 桌面工具链、系统浏览器与便携包 | [桌面开发](development/desktop.md) |
| Android 构建、真机开发及与桌面的共用／隔离边界 | [Android 开发](development/android.md) |
| 原创和第三方许可、发行未决事项 | [许可范围与发行维护](development/licensing.md)、[许可文件](../LICENSE/README.md) |

文档组织与维护约定见[开发入口](development/structure.md#文档维护)。
