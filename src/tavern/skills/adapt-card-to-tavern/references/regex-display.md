# 正则挂载 HTML / MVU 前端

这份契约适用于两种模式。用户要求补全、保留或迁移状态前端时，直接依此实现，不要求用户提供项目已有接口。用户把变量更新称为 patch note 时，结合上下文解释为 patch_state 的状态更新；正则不解析工具日志，也不要求 Writer 额外输出补丁或占位符。

## 实际运行链路

1. Runtime 将 patch_state 的 JSON Patch 应用到 stat_data，并保存完整变量快照。补丁日志不进入消息正文，也不是正则输入。
2. 正则处理消息文本，分别得到 model（模型上下文）和 display（界面展示）。开场和助手回复均使用 placement=2；用户输入为 1。展示正则仅负责挂载前端，不负责从增量补丁重建状态。
3. display 由页面、消息脚本和制卡测试共用的结构解析器统一为 `presentation.body`（消息内内容块数组）和 `presentation.panel`（独立面板数组），每块带明确的 `format` 和完整 `text`；HTML 不二次按 Markdown 解析。多个 HTML 界面及前、中、后的正文都保留，每个界面分别隔离。支持代码块和直接追加的 HTML（含完整 head/body、details 容器及相邻样式、脚本）；脚本引用 stat_data/getMvuData/getAllVariables 的面板放到右侧 MVU 区，普通 HTML 和无脚本状态栏留在消息原位置。右栏读取最新对话快照；声明 live-state 的面板在模板不变时收到更新事件，旧卡仍重建 iframe；原始 raw/model/display 不改写。
4. iframe 已注入兼容 SDK，可用 `Mvu.getMvuData({type:'chat'}).stat_data` 读取此面板的快照。`getMvuData` 不是独立全局函数；须使用 `Mvu.getMvuData`。历史消息可用 `Mvu.getMvuData({type:'message',message_id:getCurrentMessageId()})` 读取该楼层数据。右侧面板应使用 chat 快照。

展示无需修改变量或调用模型。新建纯展示面板声明 `display-only live-state`，初始化绘制并监听 `tavern_state_updated` 更新 DOM，以避免后台重演展示脚本和前端重复加载。已有卡只有确认不存在服务端回调／初始化副作用才可声明 `display-only`；未声明时继续采用原有重建方式。书架角色资料中的脚本权限用于创建新对话；已有对话使用自身保存的权限，可在 MVU 页或会话设置中开启、关闭，前后端读取同一记录。关闭时带脚本的界面明确显示已暂停，纯 HTML/CSS 仍能在禁用脚本的 iframe 中展示；开启不重演初始化、不改变变量。本地 test_draft 在隔离沙盒中执行，不能替用户开启权限。禁止网络请求或引用未缓存的 CDN；必要依赖须沿项目资源流程解析。

MVU 是变量系统，不要求全部状态栏写成 HTML。保留已正常工作的文字、Markdown 或纯 HTML/CSS 显示，不为“兼容 MVU”强制改成 JavaScript 面板。可用 `{{get_message_variable::stat_data.字段}}`（字符串原文／其他值 JSON）或 `{{format_message_variable::stat_data.字段}}`（字符串原文／其他值 YAML）；同样支持 chat、character、preset、global 作用域。宏递归省略 `$` 前缀字段，缺失值显示 null。旧式 `[值, 描述]` 用显式 `.0`／`[0]` 取值，工具补丁用 `/字段/0`；普通数组保留完整结构，不能统一剥掉第二项。当前 JS API 同时提供直接全局函数与 `TavernHelper` 命名空间；`getChatMessages().data` 为旧变量读取别名。EJS 的 `<% ... %>` 不是这些宏，仍需适配。

## 写入真实卡片字段

先完整读取当前 extensions，再用 patch_draft 写入 `extensions.regex_scripts`（V2/V3 路径 `/data/extensions/regex_scripts`，旧卡 `/extensions/regex_scripts`）。数组存在时用 `/-` 追加；缺少数组时添加数组，缺少 extensions 时先建父对象。不要覆盖原有规则及未知扩展。无需生成脚本 ID。

以下是最小可运行规则，演示读取有来源的 Money 初值；实际制作应保留原卡样式、布局和所有已映射字段，不能把本示例当成完整原面板交付：

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

这个正则在完整助手文本末尾追加一次，保留原叙事，开场和没有任何变量块的后续回复都能命中。不要用只匹配 UpdateVariable/JSONPatch/StatusPlaceHolder 的规则来渲染新回复：当前模型正文不产生它们。原卡用于隐藏开场/历史数据块的规则必须保留；原始数据供初始化使用，不因模型通道剥离就删掉原文。可以把隐藏规则放在展示挂载之前，检查两个通道的真实输出。

findRegex 是 JavaScript 正则，可以是 `/pattern/flags` 或裸 pattern（裸式默认为 g）。replaceString 是 JavaScript String.replace 替换字符串，支持 `$&`、`$1` 等引用；需要字面 `$` 时写成 `$$`，避免 HTML/JavaScript 中的替换标记意外展开。JSON 的反斜线、换行、引号遵循 JSON 转义。不要用 Python 正则语法。`markdownOnly=true,promptOnly=false` 表示仅界面；两个都为 true 会同时作用于模型和界面。

HTML 用 textContent 写入变量文本，防止把剧情字符串解释成标签；保留 null/未知与数值 0 的区别。无需把模板存到世界书、初始化块、作者备注或工具日志；这些位置不会挂载前端。普通 getvar 宏也不会在正则替换后自动成为动态 HTML 绑定。

## 必须验证的闭环

1. 回读完整 regex_scripts，运行 validate_draft；修复字段别名、类型、错误通道等提示。
2. 对每个开场运行 test_draft，带一段纯叙事 samples，确认新展示规则不影响模型/用户通道，原有隐藏规则继续剥离对应数据，output_display 包含实际前端。不能要求已有隐藏规则的 output_model 与原始开场逐字相同。没有正则时检查显示 skipped，只有转换结果时为 observed，不代表前端已通过。
3. 提供 display_assertions 验证真实 DOM，再提供 state_patch 与 after_text 验证状态更新后重新绘制。sample_index=0 是所选开场；1..N 是 samples。示例假设初始化 Money=0：

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

路径相对 stat_data，测试补丁只作用于临时状态，不保存到卡片或故事，不代表真实剧情已经到账。selector 必须唯一，text/after_text 比较去除首尾空白后的完整 textContent；缺失元素、未命中正则、脚本异常、静态写死旧值或读取错误路径都应修复。异步外部资源、延迟动画、完整视觉交互和真实模型判断不在此测试保证范围内。

4. 确认展示落盘在卡片扩展、开场与更新后显示均符合预期，才报告前端完成。live-state 面板还需在实际界面检查更新事件和输入保留，不能只依赖 test_draft 的重新挂载断言。需要离屏释放时提供完整 save/restore 并声明 suspend；允许改变匿名样式节点和内联资源 URL 时再声明 shared-assets。用户仍需审阅保存，并在新故事检查实际外观；旧故事不会自动替换卡片或快照。若只完成初始化和规则，必须明确前端尚未完成。
