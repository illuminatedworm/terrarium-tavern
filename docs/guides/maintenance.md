# 安装、备份与维护

Terrarium Tavern 在本机运行，提供 Windows PowerShell 和 Linux Bash 脚本。运行数据默认位于 `data/`；自定义目录时，备份、恢复和启动应使用同一路径。

## 安装与启动

源码安装准备 Python（或 Linux 上的 uv）、Git 和系统 Edge/Chrome 稳定版，在源码根目录运行；安装脚本默认下载固定的 Node.js 二进制包（含 npm）：

**Windows（PowerShell）**

```powershell
.\setup.ps1 -Proxy ''
.\点击开始.cmd
```

此命令使用直连下载。使用代理时，将 `-Proxy ''` 改为 `-Proxy '<完整的 HTTP(S) 代理地址>'`。

省略 `-Proxy` 时，Windows 安装脚本沿用调用环境中的代理，与 Linux 脚本一致；显式传入空字符串禁用安装代理。安装代理和应用内的代理设置分别生效。

**Linux（Bash）**

```bash
./setup.sh
./start.sh
```

Linux 安装沿用环境代理，也可通过 `--proxy '<完整的 HTTP(S) 代理地址>'` 指定，或用 `--proxy ''` 选择直连。已有 uv 时直接使用，否则通过 Python 在 `.cache/uv/` 中准备 uv。浏览器及其系统依赖需通过浏览器官方安装包准备。

安装脚本准备 Python 3.13 和锁定依赖、内置 Node.js，构建前端并检查系统浏览器，不下载 Chromium。
长期使用的工具链位于 `desktop-dev/`，不要按临时缓存清理。已有系统 Node.js 22+ 时可加
`-SkipNodeDownload` 或 `--skip-node-download`；启动、构建和开发脚本优先使用已准备的内置 Node。
已有 `.venv` 对 `.cache/python` 的引用不会自动搬迁，重建环境和便携包说明见[桌面开发](../development/desktop.md)。
启动后访问 [http://127.0.0.1:51273](http://127.0.0.1:51273)。

便携包已附带 Python、锁定依赖（含 Playwright 自带 Node 驱动）和界面，复用系统 Edge/Chrome 稳定版。
准备好浏览器后，解压并使用 `点击开始.cmd` / `start.ps1` 或 `./start.sh`，无需运行源码安装脚本。
Windows 包使用 embeddable Python；Linux 包要求 glibc x64。构建、系统要求和验证见[便携包说明](../development/desktop.md#便携包)。

v0.8.5 的 [GitHub Release](https://github.com/illuminatedworm/terrarium-tavern/releases/tag/v0.8.5) 附件中，`terrarium-tavern-0.8.5-windows-x64.zip` 是可直接运行的 Windows 基础包，`terrarium-tavern-0.8.5-android-arm64-build6.apk` 是 Android 安装包；旁边的 `.sha256` 用于校验下载完整性。GitHub 自动生成的 Source code 归档是源码，需要另行准备开发环境。本次不提供 Linux 二进制包，Linux 仍可按源码安装。

Android APK 使用 debug 签名，要求 Android 9+、ARM64 和 4 KiB 内存页，不支持 16 KiB 内存页设备。同签名可保留数据覆盖安装；更新前先导出备份，不要为处理签名冲突直接卸载旧应用。模型权重需单独导入，详见[Android 开发与设备范围](../development/android.md)。

Windows 默认优先 Edge、其次 Chrome，Linux 优先 Chrome、其次 Edge；启动会输出实际路径。
可用 `TAVERN_BROWSER=msedge` 或 `TAVERN_BROWSER=chrome` 明确指定，`auto` 恢复默认选择。
安装和启动缺少所选浏览器时会报错；WebView2 Runtime 不能代替它。具体设置方法见[系统浏览器](../development/desktop.md#系统浏览器)。

可选的受控桌面窗口（`-Desktop` / `--desktop`）额外在每个文档执行脚本前禁用 WebRTC；关闭窗口会取消尚未完成的任务、等待回滚并停止后端，外部链接在系统浏览器中打开。它使用所选数据目录的 `desktop-profile/` 保存独立浏览器资料和阅读布局，不读取系统浏览器的资料目录，阅读布局可通过设置数据包导出导入；CLI 完整备份不打包该目录。
切换浏览器或回退其版本前，先导出阅读布局并备份该目录；应用不会因浏览器启动失败而清除资料。

| 操作 | Windows | Linux |
|---|---|---|
| 停止服务 | `.\stop.ps1` | `./stop.sh` |
| 打开受控桌面窗口 | `.\start.ps1 -Desktop` | `./start.sh --desktop` |
| 使用其他端口 | `.\start.ps1 -Port 8001` | `./start.sh --port 8001` |
| 停止指定端口 | `.\stop.ps1 -Port 8001` | `./stop.sh --port 8001` |
| 指定数据目录 | `.\start.ps1 -Data 'D:\MyStories'` | `./start.sh --data '/path/to/My Stories'` |

默认关键词与 BM25 检索可在 CPU 上运行。使用本地向量检索或重排时，安装命令加 `-LocalRetrieval`（Windows）或 `--local-retrieval`（Linux），再在设置中下载、预热模型。这项功能需要支持 CUDA 的 NVIDIA GPU，见[检索指南](retrieval.md#本地模型与索引)。

应用内的模型 API、模型下载和角色资源下载分别配置代理，入口见[设置指南](settings.md)。

## 版本选择

当前版本为 **v0.8.5（实验阶段）**，Windows、Linux 与 Android 共用版本标签。需要固定源码版本时可检出 `v0.8.5`，或从仓库标签页下载对应源码，再运行当前系统的安装脚本。`main` 和 `linux` 是开发分支，后续提交可能超出已标记版本；v0.8.5 标签对应两者同步后的提交。

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

v0.8.0 将 Python 基准升级为 3.13。此前使用其他 Python 版本的 Windows 和 Linux 安装都需要重新运行 `setup.ps1` 或 `setup.sh`；uv 会按 `.python-version` 准备 Python 3.13，并重建不兼容的 `.venv` 和锁定依赖。先停止旧服务，不能直接沿用旧环境启动。重新安装可能下载较大的依赖；使用本地向量检索或重排的用户仍须加 `-LocalRetrieval` 或 `--local-retrieval`，以重新安装 torch 等可选检索依赖。已下载的模型权重位于数据目录中，不属于 `.venv`，不需要因这次 Python 升级重新下载。

当前数据库格式为 **schema 16**，设置文档版本为 **14**，个人数据包格式为 **6**。升级前先完整备份；schema 16 将上下文校准样本改为接口实测快照，旧样本失效，旧待续任务中的估值强制换窗标记同步失效，正文草稿和已完成工具保留，等待新请求，不回填日志或本地估值。快照按会话／工作区和用途保存来源、内容指纹及真实用量分项，不复制提示词正文；详细日志清理保留快照，删除所属数据时一并删除。设置 version 14 将旧 Half Agent `tokens` 模式迁移为消息批次，保留消息 cap 和批量大小，`off` 保持关闭；移除旧历史 token 预算和压力目标，设置页明确说明。不修改剧情、已有逐次用量或应用版本。

schema 14 保存开场白的永久锁定状态，升级前完整备份；旧对话不推断首次请求历史，保持开场白只读。新数据包保留此状态，旧数据包中的对话导入后保持锁定。schema 1–8 若包含剧情对话，会在任何写入前明确拒绝，原文件保留，并提示使用根目录的 `clear-conversations.cmd` 清理脚本。需要继续旧剧情时，请使用兼容旧版打开原数据；也可用旧版导出非对话资料，在新的空数据目录中导入。旧版个人数据包只支持导入非对话资料。没有剧情的旧数据库仍先只读核对，并在升级前生成 `before-upgrade-schema<旧版本>-settings<旧版本>-*.sqlite3` 完整备份。schema 13 将 Full Agent 恢复进度升为 version 3，分别保存普通正文和模型原生结束状态；旧版交付进度可续接，已保存轨迹保留审计，新数据包拒绝由旧程序误读；设置 version 10 新增可见的总运行时限（默认 900 秒），保留其他设置与已存提示词。设置 version 11 新增可见的单条向量索引时限（默认 60 秒，范围 1–3600 秒），两种模式分别保存；升级前同样备份，保留其他设置与剧情。设置 version 12 允许两个 Android 模型分别显式选择 CPU、OpenCL GPU 或实验性 Hexagon NPU；升级前备份，已有设备与其他配置原样保留。设置 version 13 将 Kemini 三项格式提示恢复为 Interleaving 成对输出；升级前备份，默认值和自定义值都迁移，保留首尾空白、换行及其他自定义内容。旧进度中已完成的工具和脚本副作用保留，不回滚旧数据。当前格式的备份恢复和整轮版本保留照常工作，详见[格式规则](../reference/settings-api.md#数据库与设置迁移)。

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
| 模型逐次用量与上下文实测快照 | 随所属会话或工作区保存；详细日志清理保留累计缓存命中率所需记录及上下文实测快照 |
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
