# 兼容矩阵（0.1）

“可导入”表示数据可解析。下表分别说明导入、显示和脚本宿主能力。HTML 交互生成及脚本嵌套模型请求适用于 Half Agent；Full Agent 的能力见[模式边界](../design/full-agent.md#权限与执行边界)。社区卡按实际使用的接口逐项确认。

| 能力 | 实现与边界 |
|---|---|
| V1/V2/V3 JSON | 保留原对象与未知字段；编辑后原格式导出 |
| PNG | chara / ccv3 tEXt，优先 V3；CRC 检查，保留其他 PNG chunks |
| CHARX | card.json 与内嵌资源，路径/大小限制，资源保真导出 |
| 世界书 | 内嵌、独立 JSON；保守关键词准入、辅助逻辑、常驻、概率、受原始查询约束的递归、深度、滚动注入及离开请求后概率冷却（旧持续字段保留但不执行） |
| 正则 | 原生 JavaScript RegExp；显示/模型阶段、placement 与 min/max depth |
| 原始提示词 | 角色正文、system、post history、开场与示例映射到配方 |
| 宏 | 模型文本保留 char、user、getvar / getglobalvar 读取；Helper 的 get / format / format_quoted 变量宏支持 message、chat、character、preset、global，用于模型上下文和文字、Markdown、HTML 展示；未知宏保留 |
| 剧情 Markdown | 使用 GFM；仅对汉字后紧接 `**`、内容以 `‘`、`“`、`「`、`『` 开头的粗体放宽起始边界，闭合仍遵循原解析规则；纯英文、代码、转义与存档原文不变，角色编辑预览不启用此扩展 |
| HTML 美化 | 原始 HTML 和代码块共用结构解析器，统一为正文／面板内容块，保留多个 HTML 界面及前、中、后的剧情；完整 head/body 按 HTML 文档处理；每个界面独立 sandbox iframe；CSS 的 url()/@import 和字体递归缓存后离线使用 |
| 角色脚本 | 固定 SDK、无宿主密钥；按回合重新建立，变量持久化 |
| 消息变量回调 | 在服务端重建消息 DOM realm，顺序执行 MVU 回调并等待完成 |
| MVU | 使用固定提交的上游更新和 Schema 核心；InitVar、stat_data、更新事件、旧事件名别名；分别保留直接值、嵌套对象、普通数组及旧式 [值, 描述] |
| MVU 命令 | 上游 set/add/insert/delete/move 与 JSON Patch 解析；上游错误通知提升为回合失败 |
| 变量 API | 全局函数及 TavernHelper 命名空间；chat、message、character、script、global、preset、extension 镜像；getAllVariables 按顶层键覆盖作用域，数组赋值整体替换，避免复活旧字段；所有 scope 隔离在会话分支中 |
| 消息读取 | 楼层号、负索引、latest、简单范围；返回角色、正文、variables 和 ID，旧 HTML 示例的 data 读取别名保留；message_id 省略时为 latest，取当前界面楼层需显式传 getCurrentMessageId() |
| 消息写入 | 回合脚本镜像；界面写入仅支持当前楼层，历史操作需先切换分支 |
| 世界书写入 | 受设定修改策略控制，保存为当前分支的内容草稿 |
| generate / generateRaw | Half Agent 支持当前模型、文本、取消、流式通知、覆盖和有序内容块；使用本项目模型配置 |
| 外部模块 | 下载和哈希缓存，离线 esbuild 打包静态可解析的浏览器模块 |
| 中途 system | 所有端点原位发送；能力检查仅诊断，不支持时显示实际错误 |

## 脚本权限

书架角色的脚本权限用于新建对话；已有对话保存自己的脚本权限，前端面板和后端执行器均以该记录为准。可以在右侧 MVU 页启用，或在会话设置中开启、关闭；操作即时保存，不重演初始化、不改写剧情与变量。关闭时带脚本的界面显示“面板已暂停”；纯 HTML/CSS 仍可在禁止脚本的 iframe 中展示，文字与 Markdown 状态栏也不需要开启脚本。继承进度时复制当前权限，之后各对话独立修改。制卡 agent 另存副本时继承源卡当前的脚本权限。

## MVU 数据与显示的分工

MVU 管理 `stat_data`，HTML 只是可选的展示方式。文字或 Markdown 可直接写 `生命：{{format_message_variable::stat_data.hp.0}}`；此例针对 `[45, "生命说明"]`，直接值用 `stat_data.hp`。访问数组整体会得到完整数组，不会猜测它是说明对还是物品列表。JSON Patch 对说明对中的数值使用 `/hp/0`；说明保留，数值类型仍校验。

`get_*_variable` 对字符串直接返回原文，其他值输出 JSON；`format_*_variable` 对字符串直接返回原文，其他值输出 YAML，并保留所在行的缩进前缀。支持点路径与带引号的方括号路径；缺失值为 `null`，`0`、`false` 和空数组不会被当成缺失。宏递归隐藏 `$` 前缀字段，模型侧额外过滤初始化账本、schema 和内部缓存。界面宏读取该消息的快照，右侧独立面板读取最新快照；原始数据与宏模板不改写。

普通文字、Markdown 和无 MVU 读取脚本的 HTML 留在消息原位置。含已识别 MVU 读取脚本的界面放到右栏，其他文字与 Markdown 状态展示沿原位置保留。实际页面、消息回调和制卡 `test_draft` 共用内容拆分规则。

变量更新由 Tracker 或 Full Agent 工具提交 JSON Patch，Writer 负责正文。开场初始化和受支持的脚本回调由 Runtime 校验，并与剧情原子提交。

接口依据：[MVU 教程](https://github.com/MagicalAstrogy/MagVarUpdate/blob/master/doc/tutorial.md)、[酒馆助手变量 API](https://github.com/N0VI028/JS-Slash-Runner/blob/c0196caee03955aa39e00db1a2623b2591bd3c6f/src/function/variables.ts)、[变量宏契约](https://github.com/N0VI028/JS-Slash-Runner/blob/c0196caee03955aa39e00db1a2623b2591bd3c6f/src/function/macro_like.ts)。上述接口由项目独立适配；EJS、父页面 DOM 依赖和未编译框架源码需按本宿主调整。

## 运行环境限制

### 面板资源与生命周期

纯 HTML/CSS 面板始终使用禁用脚本的沙箱，不加载兼容 SDK 或 MVU 核心。脚本面板先加载共享 SDK，调用异步 `Mvu.parseMessage` 时才加载固定上游核心；后端主运行时仍使用完整核心。面板通过公开 `Mvu` API 工作，不应直接依赖内部 `MVUReference` 全局对象。

完整历史可同步查询。后端脚本按需读取当前操作的历史，前端共享不可变历史源并延迟解析；各环境独立管理写入与回滚。`getAllVariables` 直接读取按楼层覆盖的变量视图。展示解析缓存最多 128 项、16 MiB 估算字符串，后端 HTML 拆分缓存最多 128 项、16 MiB UTF-8 源文；缓存内容限于派生展示数据，权威状态保存在快照中。

复杂面板可在 HTML 中明确声明项目扩展能力，各项独立生效：

```html
<meta name="tavern-capabilities" content="display-only live-state suspend shared-assets">
```

- `display-only`：该文档只负责前端显示和用户交互，不参与服务端变量回调、初始化或其他副作用；后台无需重建它。包含业务回调的旧卡不能添加此项。
- `live-state`：模板不变时复用 iframe，快照更新后触发 `eventOn('tavern_state_updated', (state, previous) => ...)`。处理器应重新读取变量并更新界面，不能再依赖每轮重跑顶层脚本。宏展开后 HTML 改变、切换会话或更新处理器失败时仍重建文档。
- `suspend`：离开阅读区域一段距离并稳定后，可销毁 iframe。必须提供 `window.TavernPanel.save()` 和 `restore(saved)`，保存／恢复完整的临时 UI 状态；支持返回 Promise。`save` 应停止不应继续运行的后台活动，不发起新的宿主操作；`restore` 在同步脚本与 jQuery-ready 初始化后调用。保存结果须为可 JSON 序列化数据。聚焦或有宿主请求未完成时不挂起；全局临时状态预算为 4 MiB，超限保留运行实例，不丢弃输入。恢复数据只在当前页面生命周期内保存，不代替正式存档。
- `shared-assets`：允许把匿名样式块和较大的内联图片／字体变为共享 Blob 资源。不得依赖原始 `src` 字符串、匿名 `<style>` 元素或其 CSSOM 身份。带属性的样式元素保留。Blob 对象共享底层内容，各沙箱自行生成本地 URL，仍无父页面 DOM 或网络访问权限。

旧卡沿用完整回调语义：后台按回合重建脚本环境，前端保留已访问的交互面板及其闭包、定时器和未提交输入。基础历史读取、按需核心和解析缓存仍生效，资源占用取决于卡片行为。

其他宿主约束：

| 项目 | 使用方式 |
|---|---|
| ST DOM、内部模块、完整 `getContext()`、STscript、EJS 与插件机制 | 需要改写为本项目支持的接口和展示方式 |
| `global`、`character`、`preset` 等变量 | 作为当前会话分支内的命名空间保存 |
| 脚本持久状态 | 通过变量 API 保存，后台脚本按回合重新加载 |
| 世界书位置 | 支持角色前后、作者注释、示例前后、深度和命名 outlet；复杂 JavaScript 正则需适配 Python 检索环境 |
| `generateRaw` | 支持角色描述、性格、场景、世界书、示例、历史及输入占位符，复杂预设脚本另行适配 |
| HTML 中的 ES module | 将模块放入角色脚本，或预先适配为可执行的浏览器脚本 |
| 外部资源 | 下载使用已获访问权限的资源；动态 URL、特殊 CDN 加载器和 ST 相对导入按卡片适配 |
| MVU/Zod 扩展 | 按具体接口检查固定核心与本项目宿主的组合行为 |

模型接口、思考强度及缓存参数见[模型接入参考](../reference/model-api.md)。

## 可复现样本

- `samples/雾港来信.json`：原创文本卡、中文世界书、角色提示词与未知扩展。
- `samples/星航状态卡.json`：原创美化卡、按钮生成、InitVar、能量变量与修正回调。
- 测试动态构造 PNG 和 CHARX，检查导入/导出；浏览器测试运行真实 Chromium 与 KohakuTerrarium。
- 上游差分测试比较固定输入下的 MVU 核心与适配流程输出。

样本覆盖对应的导入、运行和保存流程。排查其他社区卡时，可提供具体接口、错误记录和可复现卡片片段。

## MVU 与 Schema 适配

支持 StageDog registerMvuSchema 依赖链、getAllVariables、klona、errorCatched，以及带 transform 的界面 Schema 导出。原卡导入 MVU artifact 时使用宿主已有的固定核心，避免启动第二份 ST 框架。远程辅助模块仍按卡缓存和记录哈希；具体接口由隔离夹具检查，其他扩展按实际调用另行适配。

初始化遵循固定上游版本的覆盖语义：开场含 `<initvar>` 时，其完整对象替换角色卡世界书初值，未写字段不会从条目递归补齐；后续打开运行时不重演已处理的开场，也不补回被忽略的默认字段。没有开场 `<initvar>` 时，读取名字含 `[InitVar]` 的条目，大小写不敏感，条目即使关闭也参与初始化，并继续排除在常规模型提示之外。条目内容支持 YAML 对象及其可选的 `<initvar>`、代码块包裹。

初始化读取开场原文后，卡内正则再生成 model/display；`promptOnly` 控制模型提示通道，`markdownOnly` 控制界面通道，两者均启用时同时处理。已经正确初始化并从模型文本剥离的内联数据应保留，不因状态栏迁移而删除。MVU 上游还支持初始化后执行开场增量指令；本项目目前不自动执行原文中的 UpdateVariable/JSONPatch 增量，需已有脚本处理，并验证实际结果。初始化只读取世界书与开场原文，不支持扩展字段中的隐藏初值覆盖。

同一世界书或开场中的多个初始化片段按顺序合并，后面的数组完整替换前面的数组；与已有状态合并时只处理根字段，已有根对象或数组完整保留，不递归补缺。`tracker_mvu` 规则不用于初始化；未带 `[InitVar]` 标记的 `runtime_init` 条目仍须启用。已有对话快照不会因修改初值自动重置。

## 剧情修改与世界书行为

关键词命中须通过准入过滤；单字／泛词及重排拒绝的条目不能经递归重新进入。
中途条目与剧情共同计入窗口。编辑原文会使相关记忆来源过时，不自动重演后续变量；需从较早进度重生成或显式修改变量，见[历史操作](story-history.md)。

导入或保存含旧 `extensions.terrarium.workflow` 扩展的卡片时会报错。迁移前核对其中的有效初值，手工保存到世界书 `[InitVar]` 或开场 `<initvar>`，再移除旧扩展。初始化只读取这些可见原文，已有故事继续保留已提交状态。
