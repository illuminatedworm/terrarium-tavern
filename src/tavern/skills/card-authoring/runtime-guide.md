# 通用变量与运行兼容性

Half Agent 与 Full Agent 使用相同的卡片数据契约。普通制卡和 MVU 设计无需选择模式，也不为模式复制或迁移变量规则。

世界书原始条目的用途写入 extensions.terrarium.audience：

- writer：稳定人物、世界和叙事设定，默认值；沿常驻和检索规则注入。
- tracker_mvu：变量含义、变化条件、范围、关系和约束；授权更新变量时完整提供全部启用条目，不按关键词、概率或检索排名裁剪。
- runtime_init：确定性 InitVar 初始化数据，供程序执行，不作为模型任务规则。

开场 `<initvar>` 中的完整对象覆盖角色卡世界书初始化，不从条目递归补齐缺失字段。没有开场 `<initvar>` 时读取世界书初值；传统 `[InitVar]` 条目即使禁用也参与初始化，未带该标记的 runtime_init 条目仍按启停设置处理。这是上游明确支持的按开场区分初值的机制。共用默认值优先沿用 [InitVar] 条目，不为了规范化把有效内联数据搬走；已有对话的初始化记录不会自动重演，修改初值应在新对话中验证。

卡内正则 promptOnly/markdownOnly 决定模型/展示通道。Runtime 先从原始开场初始化，再生成 model/display，不删除原文。已经正确初始化、并在模型通道隐藏的特殊数据和正则必须保留，包括迁移正文状态栏时。通过 test_draft 检查实际转换，不能把“仅界面隐藏”当成“对模型隐藏”。

区分完整 initvar 覆盖与开场 UpdateVariable/JSONPatch 增量：上游支持初始化后叠加增量，本项目原文增量需明确的脚本或独立 initialization 路径处理，不会仅因标签存在而自动生效。工作流独立 initialization 是项目扩展，已有记录优先于原文（空串也覆盖）；不是所有 MVU 卡都必须使用。详细异常与适配流程见 references/workflow.md。

Half Agent 在正文生成后向 Tracker 提供变量规则。Full Agent 在当前有效上下文首次授权变量更新时追加规则，同一有效版本不重复注入；换窗或重建后按需补入。规则及宏值在各自任务开始时固定，任务内修改从后续任务起生效。历史中保留规则不意味着持续获得修改权限。

两种模式均用 patch_state 提交相对于 stat_data 的 JSON Patch，无变化提交 []。卡片写清业务语义，不复制工具 schema，不要求模型在故事正文中输出 UpdateVariable、JSONPatch 或其他变量命令块。把影响叙事的世界机制与机械更新条件分开，保留必要的 schema 注册、校验回调、宏和资源。

仅在能力影响方案时确认用户的兼容性目标：Full Agent 不支持脚本额外模型请求或 HTML 交互生成；普通初始化、支持的正则、schema 和回调不因此要求选择模式。遇到不支持的能力，说明具体依赖并讨论替代交互，不能仅凭静态检查声称完整兼容。已有目标只作此类兼容性依据。

适配已有卡读取 SKILL.md 和 references/workflow.md，两种模式共用流程。普通润色不迁移工作流，已有开场与内联初始化默认保留；明确要求拆分时才传 set_opening.initialization。test_draft 检查所选开场的本地初始化、脚本启动和正则；真实叙事、变量判断、其他开场及完整视觉交互需另行验收。

遇到模型逐轮生成 HTML、Markdown 或固定标签状态栏，读取 references/status-display.md，将有依据的状态迁入独立变量，并按 references/regex-display.md 把原 HTML 前端写入 extensions.regex_scripts，绑定 stat_data 后由项目变量区展示。项目只提供变量树兜底，不会从补丁日志自动重建原界面；除非用户明确仅要变量树，否则保留原视觉设计。用户明确要求迁移正文状态栏时也处理相关开场示范；普通排版和已有程序展示不因此迁移。诊断无警告不代表完整适配。

制作或补全正则、HTML/MVU 前端时必须读取 references/regex-display.md，使用其中的真实字段、可用 SDK 和 test_draft 展示断言；普通 patch_state 更新不要求用户提供额外 patch note 格式。

MVU 变量与 UI 分开：已有程序化文字、Markdown、纯 HTML/CSS 状态栏不必改为 JavaScript 面板。get_message_variable / format_message_variable 等 Helper 宏可直接读取 stat_data；旧式 [值, 描述] 与普通数组均保留，取旧值需显式索引 0，JSON Patch 也指向该值的位置。不要把所有两项数组自动压成单值。多种展示形式均需用同一 test_draft 检查完整内容和补丁后的变化。

新建纯展示面板优先采用 `<meta name="tavern-capabilities" content="display-only live-state">`，启动时绘制一次，并监听 `tavern_state_updated` 更新 DOM；这样无需每轮在后台重建展示脚本，也无需前端重载页面。仅在确认没有服务端变量回调或初始化副作用后才给已有卡添加 `display-only`，不根据代码关键词猜测。通过公开 `Mvu` API 访问变量，执行核心在 `Mvu.parseMessage` 时按需加载，不直接引用内部 `MVUReference`。

需要离屏释放的面板再声明 `suspend`，实现 `window.TavernPanel.save()` / `restore(saved)`，保存全部临时表单与界面状态；持久变量仍通过原 API 提交。允许复用资源时声明 `shared-assets`，避免依赖匿名 style 节点或内联 URL 身份。四种声明是本项目扩展，不代表原生酒馆能力；未适配的旧卡继续保留完整执行和交互生命周期。除 test_draft 外，必须在实际界面验证状态更新、未提交输入和离屏恢复。
