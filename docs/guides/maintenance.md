# 安装、备份与维护

Terrarium Tavern 在本机运行，提供 Windows PowerShell 和 Linux Bash 脚本。运行数据默认位于 `data/`；自定义目录时，备份、恢复和启动应使用同一路径。

## 安装与启动

准备 Python、Node.js 22+ 和 Git，在源码根目录运行：

**Windows（PowerShell）**

```powershell
.\setup.ps1 -Proxy ''
.\start.ps1
```

此命令使用直连下载。使用代理时，将 `-Proxy ''` 改为 `-Proxy '<完整的 HTTP(S) 代理地址>'`。

省略 `-Proxy` 时，Windows 安装脚本沿用调用环境中的代理，与 Linux 脚本一致；显式传入空字符串禁用安装代理。安装代理和应用内的代理设置分别生效。

**Linux（Bash）**

```bash
./setup.sh
./start.sh
```

Linux 安装沿用环境代理，也可通过 `--proxy '<完整的 HTTP(S) 代理地址>'` 指定，或用 `--proxy ''` 选择直连。已有 uv 时直接使用，否则通过 Python 在 `.cache/uv/` 中准备 uv。Chromium 提示系统库缺失时，运行 `.venv/bin/python -m playwright install-deps chromium`，按系统提示提供 sudo 权限。

安装脚本准备 Python 3.12 和锁定依赖，构建前端并下载 Chromium。启动后访问 [http://127.0.0.1:8000](http://127.0.0.1:8000)。

| 操作 | Windows | Linux |
|---|---|---|
| 停止服务 | `.\stop.ps1` | `./stop.sh` |
| 使用其他端口 | `.\start.ps1 -Port 8001` | `./start.sh --port 8001` |
| 停止指定端口 | `.\stop.ps1 -Port 8001` | `./stop.sh --port 8001` |
| 指定数据目录 | `.\start.ps1 -Data 'D:\MyStories'` | `./start.sh --data '/path/to/My Stories'` |

默认关键词与 BM25 检索可在 CPU 上运行。使用本地向量检索或重排时，安装命令加 `-LocalRetrieval`（Windows）或 `--local-retrieval`（Linux），再在设置中下载、预热模型。这项功能需要支持 CUDA 的 NVIDIA GPU，见[检索指南](retrieval.md#本地模型与索引)。

应用内的模型 API、模型下载和角色资源下载分别配置代理，入口见[设置指南](settings.md)。

## 版本选择

当前版本为 **v0.7.0（实验阶段）**，Windows 与 Linux 共用版本标签。需要固定源码版本时可检出 `v0.7.0`，或从仓库标签页下载对应源码，再运行当前系统的安装脚本。`main` 和 `linux` 是开发分支，后续提交可能超出已标记版本；v0.7.0 标签对应两者同步后的提交。

[版本记录](../changelog.md)列出历史标签及兼容说明。`v0.1.0`、`v0.2.0` 尚无 Linux Bash 安装脚本；Linux 支持从 `v0.3.0` 开始。标签用于固定源码，不代表已提供二进制安装包。

## 备份、升级与回退

完成或停止故事生成、参谋 agent 和制卡 agent 任务后，创建完整备份：

```powershell
.venv/Scripts/python.exe -m tavern --data data --backup backups/story.zip
```

Linux 将上述 Python 路径替换为 `.venv/bin/python`，下文恢复命令同理。每次备份使用新文件名。

备份包含数据库、角色资源、已缓存依赖和运行文件，包括剧情、API 密钥、编辑草稿、撤销记录和参谋咨询，应作为私有数据保存。模型权重单独管理；ChatGPT 登录凭据绑定当前安装，恢复或换机后重新连接账号。

恢复时指定新的空目录，程序核验清单与哈希后写入：

```powershell
.venv/Scripts/python.exe -m tavern --data restored-data --restore backups/story.zip
.\start.ps1 -Data restored-data
```

Linux 使用 `./start.sh --data restored-data` 启动恢复的数据。

升级步骤：

1. 保留升级前的完整备份。
2. 将新版源码或发行包放入新程序目录，安装依赖。
3. 用 `-Data` 或 `--data` 指向原数据目录并启动。

当前数据库格式为 **schema 11**，设置文档版本为 **8**，个人数据包格式为 **4**。schema 1–8 若包含剧情对话，会在任何写入前明确拒绝，原文件保留，并提示使用根目录的 `clear-conversations.cmd` 清理脚本。需要继续旧剧情时，请使用兼容旧版打开原数据；也可用旧版导出非对话资料，在新的空数据目录中导入。旧版个人数据包只支持导入非对话资料。没有剧情的旧数据库仍先只读核对，并在升级前生成 `before-upgrade-schema<旧版本>-settings<旧版本>-*.sqlite3` 完整备份。当前格式的备份恢复和整轮版本保留照常工作，详见[格式规则](../reference/settings-api.md#数据库与设置迁移)。

需要清空剧情对话并保留现有资料时，先停止酒馆服务。Windows 默认数据目录可双击根目录的 `clear-conversations.cmd`，按提示输入 `y`；自定义目录必须传入与启动时一致的路径：

```powershell
.\clear-conversations.cmd --data 'D:\MyStories'
```

Linux 使用 `.venv/bin/python clear-conversations.py --data '/path/to/My Stories'`。加 `--dry-run` 只预览，加 `--yes` 跳过交互确认。脚本不绑定某个版本，会删除所选目录的**全部剧情对话**，包括当前版本的对话、分支、未完成和待审核回合；角色卡、用户角色、世界书库（含角色卡的新建对话默认项）、设置、制卡资料与仍被引用的资源保留。

脚本在完整副本中复用程序的升级与删除入口，并清理关联检索缓存和请求调试日志；校验成功后才替换原目录。复制需要足够容纳原数据目录的空闲磁盘空间。完整原目录保存在脚本显示的备份路径下的 `original/` 中，包含剧情、密钥和资源。完成后可按原方式启动；需要回退时，先停止服务，将当前数据目录另行改名，再把 `original/` 移回原数据路径，并使用兼容旧版打开旧剧情。无法识别、升级或完整清理的格式会报错，原数据保留。

## 清理与资源保留

在“设置 → 通用 → 数据与存储”查看占用和清理预览。修改限额后，等待保存成功再刷新预览。

| 数据 | 默认保留方式 |
|---|---|
| 详细工作日志 | 最近 14 天、上限 512 MiB；运行中、待审核及可重试失败回合受保护 |
| 剧情、分支、快照 | 随会话持续保存 |
| 上下文与工具轨迹 | 随所属会话或编辑工作区保存 |
| 模型逐次用量 | 随所属会话保存；详细日志清理保留累计缓存命中率所需记录 |
| 检索索引 | 上限 256 MiB；先清无引用旧集合，再淘汰可重建缓存 |
| 模型权重 | 在本地模型设置中手动管理 |
| 编辑草稿与撤销历史 | 随制卡对话保存 |

日志清理将详细请求和过程压缩为紧凑活动记录。受保护内容可能使实际占用暂时高于限额；手动清理可在空闲时收缩数据库文件。参谋 agent 和制卡 agent 运行期间需要先完成或停止任务。

角色资源由角色卡、故事副本和编辑草稿共同引用。删除对象时，仅回收失去全部引用的资源；删除会话会一并清除其正文、回合、工具轨迹及独占索引。

## 常见问题

| 现象 | 检查与操作 |
|---|---|
| 找不到 Python、npm 或 Git | 安装前置工具后重新打开终端 |
| 下载失败 | 检查安装代理和目标站点连接 |
| 启动提示未构建 | 执行 `.\build.ps1` 或 `./build.sh` |
| 端口占用 | 停止旧服务，或指定其他端口 |
| Half Agent 记忆失败 | 检查 Tracker 模型、密钥、代理及预算，再手动整理，见[窗口与记忆](history-maintenance-settings.md) |
| Full Agent 无法继续 | 检查工具开关、调用额度和交接笔记，见[失败恢复](full-agent.md#失败恢复与排查) |
| 预计输入过大 | 查看[上下文内容占比](token-counting.md#查看占比) |
| 世界书未注入或本地模型不可用 | 按[检索排查顺序](retrieval.md#排查未命中与误注入)检查 |
| 详细事件已过期 | 查看紧凑活动记录和已保存剧情 |

开发启动与测试命令见[开发验证与发行](../development/validation.md)。
