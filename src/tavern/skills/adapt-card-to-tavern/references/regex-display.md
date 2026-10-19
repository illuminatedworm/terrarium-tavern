# 正则挂载 HTML / MVU 前端

制作、保留或迁移状态前端时按本契约实现，项目已有接口无需用户另行提供。用户把变量更新称为 patch note 时，指的是 `patch_state` 的状态更新；正则不解析工具日志，Writer 也不额外输出补丁或占位符。

## 运行链路

1. Runtime 把 `patch_state` 的 JSON Patch 应用到 `stat_data`，保存完整变量快照。补丁日志不进入消息正文，也不是正则输入。
2. 正则处理消息文本，分别得到模型文本与展示文本；开场和助手回复用 `placement: [2]`，用户输入用 1。展示正则负责挂载前端，前端读取已提交的快照。
3. 展示文本被拆成消息内内容块与独立面板，每个 HTML 界面在各自的 iframe 中隔离，前、中、后的正文都保留。脚本引用 `stat_data`、`getMvuData` 或 `getAllVariables` 的面板放到右侧 MVU 区并读取最新对话快照；普通 HTML 和无脚本状态栏留在消息原位置。
4. iframe 已注入兼容 SDK。右侧面板用 `Mvu.getMvuData({type:'chat'}).stat_data` 读取快照；`getMvuData` 只能通过 `Mvu` 调用。历史楼层用 `Mvu.getMvuData({type:'message',message_id:getCurrentMessageId()})`。当前 JS API 同时提供全局函数与 `TavernHelper` 命名空间；`getChatMessages().data` 是旧变量读取别名。通过公开的 `Mvu` API 访问变量，执行核心在调用 `Mvu.parseMessage` 时按需加载，内部的 `MVUReference` 不作为接口。

前端只读取变量，不调用模型。网络请求和未缓存的 CDN 不可用，必要依赖须按项目资源流程缓存。文字与 Markdown 展示可直接用变量宏（见 `runtime-guide.md`），不必改成 JavaScript 面板。

## 写入卡片字段

1. 完整读取当前 `extensions`。
2. 用 `patch_draft` 写入 `extensions.regex_scripts`（包装卡 `/data/extensions/regex_scripts`，旧卡 `/extensions/regex_scripts`）：数组已存在时用 `/-` 追加；缺数组时先添加数组，缺 `extensions` 时先建父对象。原有规则和未知扩展保持不变，无需生成脚本 ID。
3. 原卡用于隐藏开场或历史数据块的规则保留，可以放在展示挂载规则之前。展示规则可能有多条（不同面板、开场与后续回复、深度条件），新增规则不替换已有规则，逐条核对命中与顺序。

以下是最小可运行规则，演示读取有来源的 Money 初值。实际制作要保留原卡的样式、布局和所有已映射字段：

```json
{
  "scriptName": "MVU 状态面板",
  "findRegex": "/$(?![\\s\\S])/",
  "replaceString": "\n<meta name=\"tavern-capabilities\" content=\"display-only live-state\"><div id=\"mvu-status\">余额：<span id=\"money\"></span></div><script>const render=()=>{document.getElementById('money').textContent=String(Mvu.getMvuData({type:'chat'}).stat_data.Money ?? '未知');};render();eventOn('tavern_state_updated',render);</script>",
  "placement": [2],
  "disabled": false,
  "markdownOnly": true,
  "promptOnly": false,
  "minDepth": null,
  "maxDepth": null
}
```

这条规则在完整助手文本末尾追加一次，保留原叙事，开场和后续回复都能命中。当前模型正文不产生 UpdateVariable、JSONPatch 或 StatusPlaceHolder，所以挂载规则要匹配文本末尾，而不是匹配这些块。

## 正则语法

- `findRegex` 是 JavaScript 正则，可写成 `/pattern/flags` 或裸 pattern（裸式默认 g）。
- `replaceString` 是 JavaScript `String.replace` 的替换串，支持 `$&`、`$1`；字面 `$` 写成 `$$`，避免 HTML/JavaScript 中的 `$` 被当作替换标记。
- JSON 中的反斜线、换行和引号按 JSON 转义。
- 通道取值见 `runtime-guide.md` 的“正则与通道”；展示规则用 `markdownOnly: true, promptOnly: false`。

## 前端写法

- 用 `textContent` 写入变量文本，避免剧情字符串被解释成标签；保留 null/未知与数值 0 的区别。
- 模板直接写在展示正则里。世界书、初始化块、作者备注和工具日志都不会挂载前端；普通 `getvar` 宏在正则替换后也不会变成动态绑定。
- 面板能力声明写在 `<meta name="tavern-capabilities" content="…">` 中，是本项目扩展，原生酒馆不识别：
  - `display-only`：面板只展示，后台不重演其脚本。已有卡确认不存在服务端回调和初始化副作用后才可添加，不根据代码关键词猜测。
  - `live-state`：模板不变时复用 iframe，快照更新后触发 `eventOn('tavern_state_updated', (state, previous) => …)`，处理器重新读取变量并更新 DOM。新建纯展示面板使用 `display-only live-state`，启动时绘制一次并监听该事件。
  - `suspend`：离屏时可销毁 iframe，须实现 `window.TavernPanel.save()` / `restore(saved)`，保存全部临时表单与界面状态；持久变量仍通过原 API 提交。
  - `shared-assets`：允许复用资源，不依赖匿名 style 节点或内联 URL 身份。
  - 未声明的旧卡保留完整的执行与交互生命周期。
- 带脚本的面板需要对话开启脚本权限；关闭时显示“面板已暂停”，纯 HTML/CSS 仍在禁用脚本的 iframe 中展示。

## 验证闭环

1. 回读完整 `regex_scripts`，运行 `validate_draft`，修复字段别名、类型和通道提示。
2. 对每个开场运行 `test_draft`，带一段纯叙事 `samples`：确认展示规则不影响模型和用户通道，原有隐藏规则继续剥离数据，`output_display` 包含前端。有隐藏规则时，`output_model` 与原始开场不同是正常的。无正则时结果为 skipped，只有转换结果时为 observed，二者都不代表前端已通过。
3. 提供 `display_assertions` 验证真实 DOM，再提供 `state_patch` 与 `after_text` 验证更新后重新绘制。`sample_index=0` 是所选开场，1..N 是 samples。示例假设初值 Money=0：

```json
{
  "card_ref": "C1",
  "index": 0,
  "samples": ["客人推门进来。"],
  "display_assertions": [
    {"sample_index": 0, "selector": "#money", "text": "0", "after_text": "12"},
    {"sample_index": 1, "selector": "#money", "text": "0", "after_text": "12"}
  ],
  "state_patch": [{"op": "replace", "path": "/Money", "value": 12}]
}
```

路径相对 `stat_data`；测试补丁只作用于临时状态，不保存到卡片或故事。`selector` 必须唯一，`text`/`after_text` 与去除首尾空白后的完整 textContent 比较。缺失元素、未命中正则、脚本异常、写死旧值或读取错误路径都需修复。异步外部资源、延迟动画、完整视觉交互和真实模型判断不在测试范围内。

4. 开场与更新后显示都符合预期，才报告前端完成；只完成初始化和规则时，明确说明前端尚未完成。`live-state` 和 `suspend` 面板还需在实际界面检查更新事件、未提交输入和离屏恢复。用户审阅保存后，在新故事检查实际外观；旧故事不会自动替换卡片或快照。
