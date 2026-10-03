# 设置持久化与 API

本文供修改设置客户端、迁移逻辑和配置存储的开发者使用。界面操作见[设置指南](../guides/settings.md)，模型传输字段见[模型接入参考](model-api.md)。

## 配置范围与修订号

数据库中的设置文档版本为 2，由三个独立范围组成：

| 范围 | 主要内容 | 模型字段 |
|---|---|---|
| `full_agent` | 模型、工具与记忆、检索、提示词及默认权限 | `model` |
| `half_agent` | 模型、历史与记忆、检索、提示词、上下文配方及默认权限 | `foreground`、`background` |
| `shared` | 提供商与密钥、角色卡编辑模型、本地模型文件、网络和清理参数 | `card_editor` |

每个范围有自己的 `revisions` 值。更新共享提供商可能使其他范围中的模型能力检查失效，因此一次响应可能包含多个范围的新修订号。客户端应合并响应中的实际值，不能只递增提交范围的本地计数。

`SettingsDocument.runtime(mode)` 生成一次操作的深拷贝配置。正在执行的回合继续使用开始时的快照；保存后的配置用于后续操作。Full Agent 与 Card Editor 都有明确模型配置，不在运行时继承 Half Agent 模型。

## 读取与写入

`GET /api/settings` 返回 `version`、三个配置范围、`revisions`、设置页面目录 `pages` 和提示词目录 `prompt_definitions`。提供商仅返回 `has_api_key`，不返回密钥原文。

`PATCH /api/settings/{scope}` 接受：

| 字段 | 必填 | 含义 |
|---|---|---|
| `expected_revision` | 是 | 从最新读取结果取得的该范围修订号 |
| `changes` | 是 | 相对于该范围的字段路径到新值的映射 |
| `edit_session` | 否 | 将修改关联到一个可撤回的设置编辑会话 |

例如，读取到 `revisions.full_agent` 为 3 后，调整 Full Agent 输出预算：

```http
PATCH /api/settings/full_agent
Content-Type: application/json

{
  "expected_revision": 3,
  "changes": {"model.reply_tokens": 4096}
}
```

成功响应为完整的公开设置文档。下一次修改使用响应中的修订号。数组整体更新；更新 `shared.providers` 时应保留其余提供商。已有提供商的 `api_key` 留空或省略表示保留密钥，不能通过空字符串清空它。

字段路径使用点分形式，例如 `model.reply_tokens`，不是 JSON Pointer。提供商引用、预算关联及字段类型在整个文档上统一校验，失败时不写入部分配置。`capability` 和 `capability_key` 由连接检查维护，客户端不能通过普通设置写入伪造其值。旧扁平 `PUT /api/settings` 不受支持。

常用偏好保存在 `shared.preference_presets`，默认为空数组。每项包含唯一的 `id`、`name`（最多 80 个字符）和 `content`；三者不能仅包含空白，偏好原文保留换行和空格。通过 `PATCH /api/settings/shared` 整体更新列表，设置页标识为 `shared.preference_presets`，支持修订检查和页级撤回。常用偏好只作为可复用内容，不自动关联或更新会话的 `turn_preference`。

## 消息适配

模型配置的 `message_adapter` 字典整体写入，例如 `model.message_adapter`、`foreground.message_adapter`。结构为 `{"version":1,"mappings":{"chat_completions":{"state":{"role":"user","wrap":false}}}}`；省略类型使用程序默认值。只允许覆盖类型目录中 `configurable=true` 的项。

`GET /api/message-adapter/types?lane=foreground` 返回该调用链的类型目录、标签和四种协议的默认规则；`lane` 也可为 `background`、`full_agent`、`card_editor`。

对话配置使用 `GET/PUT /api/sessions/{sid}/message-adapter`；制卡使用 `GET/PUT /api/card-editor/conversations/{wid}/message-adapter`。GET 返回 `revision`、按调用链组织的 `config` 和 `lanes` 类型目录。PUT 提交 `{"expected_adapter_revision":0,"config":{...}}`，成功返回完整的新值。这个修订号独立于剧情／草稿修订号，运行中也可保存。下一次模型调用读取新值，同次调用的重试保持原快照。

全局 adapter 默认值只在创建对话时复制，分支复制父对话当前设置；全局修改不影响已有对话。持久化保存在 `message_adapter_configs`，随对话删除和备份管理。

## 页级撤回

1. `POST /api/settings/edit-sessions` 返回 `id` 和当前 `settings`，服务器同时保存基线快照。
2. 后续 PATCH 携带这个 `edit_session`，服务器记录该编辑会话最近写入的值。
3. `POST /api/settings/edit-sessions/{id}/undo` 接受页面标识和该页面所属范围的修订号，例如：

   ```json
   {"page": "full_agent.model", "expected_revision": 4}
   ```

4. `DELETE /api/settings/edit-sessions/{id}` 释放快照。

页面标识取自 `pages[].id`。撤回将该页字段恢复为打开编辑会话时的值，包括服务器保存的密钥；密钥不进入响应。服务器还会比较当前值与本编辑会话最近写入的值，防止撤回覆盖其他窗口的修改。

快照保存在服务端内存中，闲置两小时过期，服务重启后失效。撤回只处理设置值，不回滚已经执行的生成、下载、预热或清理任务。

## 错误处理

| 状态 | 典型原因 | 客户端处理 |
|---|---|---|
| 400 | 未知字段、非法关联、试图写入能力检查字段 | 保留草稿并显示校验信息 |
| 409 | 修订冲突、编辑会话过期、撤回会覆盖外部修改 | 重新读取设置或重新打开编辑会话 |
| 422 | 请求体或路径参数不符合接口类型 | 修正请求结构 |

错误信息不回显提交的密钥。冲突后的重试必须先取得新版本，不能盲目替换 `expected_revision` 后覆盖其他修改。

## 数据库与设置迁移

数据库 schema 与设置文档当前都为 2；剧情快照中的 `memory_document.version` 为 1，属于独立的数据格式。

打开 schema 1 数据库时，存储层先只读检查版本和配置，再在数据目录生成 `settings-before-v2-*.sqlite3` 备份，随后迁移设置；编辑工作区事务迁移完成后写入 schema 3。schema 1/2 升级前另生成 `before-card-editor-v3-*.sqlite3` 备份，旧编辑对话保持原身份。未知数据库版本在执行建表操作前被拒绝。

迁移按升级前的实际生效值创建独立设置：继承 Writer 的 Full Agent 模型、继承 Tracker 的 Card Editor 模型会被复制为明确配置；共用运行参数复制到两种模式。已有提供商、密钥和自定义文本保留。旧程序回退应使用迁移前备份，不能直接打开已升级数据库。迁移范围是设置和数据库格式，不代表任意历史程序版本的对话语义都受支持。

其他配置迁移由同一设置层管理：

- 旧记忆与设定维护提示词中的自定义原则按用途合并到 `tracker_system_prompt`；默认提示词由程序更新。
- 旧提供商中的协议和路径迁移到引用它的模型；模型独立保存接入方式。
- 已保存应用提示词中的“玩家”和英文 `player` 规范为“用户”；角色卡、用户角色名称和剧情原文不参与替换。实际发生变化的范围才增加修订号。
- 缺少缓存模式的配置在迁移前保存数据库备份，正文使用本地控制，后台和角色编辑使用云端策略；已有会话的手动断点及跟进参数保留。

迁移重复执行不覆盖已迁移的配置。升级操作与恢复步骤见[安装与维护](../guides/maintenance.md#备份升级与回退)。

## 实现与验证

| 入口 | 职责 |
|---|---|
| [core/settings.py](../../src/tavern/core/settings.py) | 设置模型、迁移、运行时投影 |
| [application/settings_api.py](../../src/tavern/application/settings_api.py) | 页面目录、修订校验、公开视图和撤回 |
| [storage/store.py](../../src/tavern/storage/store.py) | 数据库版本检查、备份与持久化 |
| [useSettingsEditor.ts](../../web/composables/useSettingsEditor.ts) | 保存队列、草稿与响应合并 |
| [test_settings_document.py](../../tests/backend/test_settings_document.py) | 模式隔离、迁移、密钥保护与并发行为 |
