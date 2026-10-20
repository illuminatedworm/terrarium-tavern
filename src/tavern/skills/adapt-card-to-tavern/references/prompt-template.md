# 改写 ST-Prompt-Template（EJS）语法

卡片含 `<% … %>`、世界书标题前缀 `[GENERATE:…]`、`[RENDER:…]`、`@INJECT`、`[InitialVariables]`、`[Preprocessing]` 或条目开头的 `@@` 装饰器时读取本文件。这些语法来自 SillyTavern 扩展 [ST-Prompt-Template](https://github.com/zonde306/ST-Prompt-Template)。本项目不执行它们，原文会作为普通文本进入模型或界面，所以必须按作者意图改写为本项目机制，而不是逐句翻译代码。

## 插件如何运行

- **生成前**：酒馆拼好整份提示词后，插件执行其中所有 `<% … %>`，用输出替换代码块，再发给模型。
- **渲染后**：对楼层 HTML 再执行一次，可执行模型输出里的 `<% setvar(…) %>`，并按 `[RENDER]` 条目追加界面。
- **变量**：`variables` 与 `getvar(路径)` 默认读取合并视图，当前楼层的消息变量覆盖聊天变量和全局变量。MVU 的 `stat_data` 存在消息变量中，因此 `getvar('stat_data.世界.地点')` 对应本项目的 `stat_data.世界.地点`；不以 `stat_data.` 开头的路径是插件自有变量，不属于 MVU。
- **标签**：`<%` 执行，`<%=` 转义输出，`<%-` 原样输出，`_`/`-` 变体只影响空白，`<%#` 是注释，`<%%` 输出字面 `<%`；`<#escape-ejs>…<#/escape-ejs>` 内的标签不执行。
- **启停反转**：插件有一项兼容设置，开启时特殊条目（`[GENERATE]`、`[RENDER]`、`@INJECT`、`@@generate_*`、`@@render_*`）只在**关闭**时生效。所以这类条目的 `enabled` 不能说明作者意图，要按内容判断它是否在用。

## 步骤

1. 完整读取并列出每处插件语法的位置和意图：插值、条件文本、状态栏、初值、变量写入、注入位置，或生成时的副作用。
2. 先按 MVU 三要素归位：初值进入标准初始化，变量变化进入 `tracker_mvu` 规则，展示进入展示正则。要求模型输出状态栏的段落，无论是否包在条件里，都按 `references/status-display.md` 迁移，不需要转换条件。
3. 其余内容按下面的映射改写。改写后原位置不再保留插件语法。
4. 不能可靠改写的保留原文，列为未转换项，说明影响：插件语法会原样进入模型文本或界面。不擅自删除作者逻辑，删除或改设计前先和用户商定。
5. 报告时逐处写明位置、原用途、改写方式和可靠性（等价、近似、降级、未转换）。有未转换项时报告为部分适配；降级项要逐项说明行为差异。

## 等价或近似改写

| 插件用法 | 改写为 | 可靠性 |
|---|---|---|
| `<%- getvar('stat_data.x') %>`、`<%= variables.stat_data.x %>`、`print(…)` 输出变量 | 模型文本用 `{{getvar::stat_data.x}}`；展示用 `{{format_message_variable::stat_data.x}}` 或 `get_message_variable` | 等价（只读）。放在角色字段或常驻条目会使请求前缀随取值变化；Half Agent 的 Writer 每轮都能看到完整变量，通常删掉插值、写清变量含义即可 |
| `[InitialVariables]` 标题、`@@initial_variables` 装饰器 | 标题改为 `[InitVar] …`，正文保留 YAML/JSON 对象，删除装饰器行 | 等价。插件初值位于变量根，本项目初值位于 `stat_data` 下；正文顶层若是 `stat_data:` 则去掉这一层，其他位置引用这些变量的路径统一补上 `stat_data.` |
| `userName`、`charName`、`assistantName` | `{{user}}`、`{{char}}` | 等价 |
| `<#escape-ejs>` 包裹、`<%%` 转义 | 去掉包裹标签，`<%%` 改为字面 `<%` | 等价 |
| `@@activate` | 条目设为常驻 | 等价 |
| `@@dont_activate` | 条目关闭 | 等价 |
| `@@always_enabled` | 条目启用 | 等价 |
| `@@private`、`@@preload`、`@@only_preload`、`@@dont_preload`、无法识别的 `@@xxx` | 删除装饰器行。条目只为预加载执行代码时，见“未实现” | 等价 |
| `[RENDER:BEFORE/AFTER]`、`@@render_*`、`@@iframe`、`@@message_formatting` 面板 | 按 `references/regex-display.md` 写展示正则和 HTML 面板。`@@if !is_user && !is_system` 对应 `placement: [2]`；`@@iframe 标题` 的折叠效果用 `<details><summary>标题</summary>`；面板里的 `<%- … %>` 改为变量宏，或用面板脚本 `getAllVariables()` 读取 | 等价或近似；面板脚本需要用户开启脚本权限 |
| 渲染时的显示计算（格式化数值、按变量选择显示文本） | 展示面板脚本读取变量后计算 | 近似；只影响显示，不进入模型文本 |
| `activateRegex(模式, 字符串)` | `extensions.regex_scripts`：选项 `generate` 对应 `promptOnly`，`message` 与 `html` 对应 `markdownOnly`，`placement` 按作用对象填写 | 等价（仅字符串替换） |
| 模型输出 `<% setvar/incvar/decvar/delvar/insvar/patchVariables %>`，或要求模型这样输出的指令 | 删除输出要求，把每个变量何时变化、何时保持写进 `tracker_mvu` 规则 | 近似：数值由变量任务判断后提交，不再由代码确定计算；变量只在用户勾选“本轮更新变量”时更新 |
| `[GENERATE:BEFORE]`、`@@generate_before` 中的普通文本 | 启用的常驻条目，`position: 0` | 近似 |
| `[GENERATE:AFTER]`、`@@generate_after`、`@INJECT pos/target … role=` | 启用的常驻条目，`position: 4`，按原位置换算 `depth` 并设置 `role` | 近似：只在 Half Agent 的“按角色卡位置注入”方案中按位置生效；“随对话滚动”与 Full Agent 都不按条目位置排列 |
| `@INJECT regex=`、`[GENERATE:REGEX:模式]` | 普通条目，把模式改为关键词或关键词正则 | 近似：本项目关键词还要通过准入过滤；`matched_message` 等插值无法保留 |
| `injectPrompt(键, 文本)` 配合 `getPromptsInjected(键)` | 文本写入 `position: 7` 的条目，`outletName` 填原键 | 近似：出口位置由用户在会话配方中添加 `outlet:键` 块，卡片无法自带；接收端在预设中时提醒用户手动配置 |

## 低可靠改写（降级）

以下用法只能把程序判断改成模型判断，行为会变，必须在报告中逐项说明：

| 插件用法 | 改写为 | 行为差异 |
|---|---|---|
| `@@if 条件`，或整条正文包在 `<% if (…) { %> … <% } %>` 中 | 正文保留，开头写明适用条件，例如“仅当 stat_data.世界.战斗判断 为 true 时适用以下规则”；条件与剧情用词强相关时，也可改为关键词触发 | 插件在条件为假时不发送这段文本；改写后文本常驻，由模型自行判断是否适用，可能误用，也会多占 tokens |
| `<% if %>` 多分支选择不同文本（好感阶段、事件阶段） | 合并为一张分级说明，如“好感 0–20：……；21–50：……”，放在 `writer` 条目 | 全部分支同时可见，模型按当前变量选用；分支越多越容易混用 |
| `getwi(条目)` 按条件加载、`activewi(条目)`、`@@preprocessing` / `[Preprocessing]` 预处理激活 | 被加载的条目改为普通 `writer` 条目（关键词或常驻），并写明适用条件 | 失去按变量确定激活；关键词条目还要经过检索准入 |

Full Agent 的变量不自动进入上下文，模型需要用 `read_state` 读取，条件改写在该模式下更不可靠。卡片以 Full Agent 为目标时要向用户说明。

## 目前未实现

本项目没有以下能力的对应机制，不能靠改写获得等价行为：

- **按变量确定性门控**：条件为假时文本完全不进入请求。
- **生成时计算**：循环拼接文本、数值公式、`faker` 与 `Math.random` 随机、`Date` 时间，以及 lodash 运算后写进提示词的结果。
- **生成或渲染时的变量副作用**：每次生成执行的 `setvar/incvar`、预加载阶段的 `define`、计数器。
- **`define` 跨条目共享**的函数和常量。
- **宿主操作**：`execute()` 执行 STscript、`SillyTavern.getContext()`、`getchar/getpreset/getqr` 读取角色、预设与快速回复。
- **`activateRegex` 函数式替换**作用于模型文本。
- **“处理原始消息内容”**：永久改写楼层原文。本项目原文不变，正则只派生模型文本与展示文本。
- **精确的消息结构操作**：把独立 `role` 消息插到第 N 条消息（`[GENERATE:{idx}:…]`）或按内容匹配的位置。只能用 `depth` 近似。
- **插件内置的 token 统计变量**（`LAST_SEND_TOKENS` 等）。

遇到这些用法时，先看能否换一种设计：把计算结果改为 Tracker 维护的变量（例如“好感阶段”由变量任务按规则更新，Writer 直接读取），或者把只影响显示的逻辑移到展示面板脚本。这属于重新设计，要和用户确认后再做；不能确认时保留原文，列为未转换项。

## 示例

原条目（常驻，`position: 4`）：

```text
<%_ if (getvar('stat_data.世界.战斗判断') === true) { _%>
战斗时在每次回复尾部输出 <fighting_info> 状态栏……
<%_ } _%>
```

这是“要求模型输出状态栏”，按 `references/status-display.md` 迁移：战斗数据改为变量，规则写入 `tracker_mvu`，`<fighting_info>` 面板改为读取变量的展示正则，原条目的输出要求删除。条件随之消失，不属于降级。

原条目：

```text
<% if (getvar('stat_data.林默.好感') > 50) { %>林默把 {{user}} 当作朋友。<% } else { %>林默仍对 {{user}} 保持戒备。<% } %>
```

改写为 `writer` 条目，报告为降级：

```text
林默对 {{user}} 的态度取决于 stat_data.林默.好感：大于 50 时把 {{user}} 当作朋友，否则仍保持戒备。
```
