---
name: adapt-card-to-tavern
description: 按 Terrarium Tavern 的卡片格式和游玩规则适配角色卡。适用于用户要求项目适配或正文状态栏迁移的任务。
---

# 适配角色卡

剧情模型使用卡片中的叙事设定写正文，在用户授权时读取标记为 `mvu_rules` 的规则并更新变量。程序从开场和世界书初始化变量。展示正则挂载面板，面板读取当前变量。与 SillyTavern 的差异见 `runtime-guide.md` 的“与 SillyTavern 的异同”。

涉及 MVU 的卡，无论原来如何实现，适配后都必须具备三项要素：**标准初始化**、**`mvu_rules` 变量规则**、**展示正则**。其余步骤都服务于这三项。

## 读取顺序

1. `runtime-guide.md`：与 ST 的异同、三项要素、初始化、变量规则、正则与展示。
2. `references/workflow.md`：初始化与开场的保留规则和核对清单。
3. `references/status-display.md`：卡片要求模型逐轮输出 HTML、Markdown 表格或固定标签状态块，或用户要求迁移这类展示时读取。
4. `references/regex-display.md`：制作、保留或迁移展示正则时读取。
5. `references/prompt-template.md`：卡片含 `<% … %>`、`[GENERATE`/`[RENDER`/`@INJECT` 等标题前缀或 `@@` 装饰器（ST-Prompt-Template 语法）时读取。

## 步骤

1. **定位与读取**：从草稿概览和用户点名的内容开始。用 `search_entries` 在世界书中搜索相关名称、标记和用语（如 `HTML`、`[InitVar]`、`mvu_rules`、状态栏），再按路径完整读取命中的相关条目。用 `overview` 的 `path` 查扩展结构，只读取相关正则或脚本。需要检查的角色字段、开场和示例同样按字段读取。整卡适配也按实际要处理的来源分批核对，先完成一个一致的子集，再继续剩余部分。
2. **盘点三项要素**：分别记下初值、变量规则、展示正则现在在哪、是否标准、缺什么。
3. **初始化**：标准来源原位保留（`references/workflow.md`）；非标准来源按 `runtime-guide.md` 的“非标准初始化”改成标准形式并提醒用户。
4. **变量规则**：用 `patch_draft` 标记本次处理的规则条目，叙事条目沿用已有用途或默认的 `narrative`。混合条目拆分时保留原条目的叙事，新规则条目省略 ID；需要 `split_from` 时只用读取工具给出的短编号，程序恢复真实关联。按“规则条目要求”确保有有效的 `mvu_rules` 条目，并按“局部更新语义”为每个值写清何时变化、何时保持旧值。
5. **改写输出要求**：检查角色字段、`system_prompt`、`post_history_instructions`、示例和常驻条目，删除或改写要求游玩模型在正文中输出变量块或状态栏的指令。
6. **展示正则**：保留已有的展示和隐藏正则，核对与本次改动有关的匹配范围与顺序。正文状态栏按 `references/status-display.md` 迁移，缺少展示时按 `references/regex-display.md` 新建。
7. **开场**：叙事不改，不调用 `set_opening`，以下两种情况除外：改写非标准初始化时只改其中的初始化数据；用户明确要求迁移正文状态栏时，按 `references/status-display.md` 处理可见示范。
8. **脚本**：schema 注册、校验与更新回调、宏和资源原样保留。不猜测复杂动态脚本产生的初值，也不把 arbitrary JavaScript 改写为初始化数据。无法可靠迁移的逻辑保留原状，列为人工核对项。ST-Prompt-Template 语法按 `references/prompt-template.md` 改写，并标明可靠性。
9. **验证**：`validate_draft` 并修复可确定的问题；对每个受影响的开场运行 `test_draft`，按 `references/regex-display.md` 提供 `display_assertions` 与 `state_patch`。核对返回的实际状态，工具无异常不等于语义正确。
10. **报告**：按三项要素分别列出改动、涉及条目和验证结果，再列开场是否修改、未覆盖范围和待处理项。

## 完成判定

同时满足以下各项才报告适配完成，否则报告为部分适配并列出剩余项：

- 初始化：全部来自标准形式，每个开场的初始化来源、实际状态与隐藏效果已核对（见 `references/workflow.md`）。
- 变量规则：有有效的 `mvu_rules` 条目，每个值都写明更新与保持条件，叙事与规则已分流。
- 展示正则：开场与变量变化后的展示已通过对应测试。
- 正文不再被要求输出变量块或状态栏，且无待处理问题。

工具只编辑草稿，原卡由用户在差异页审阅保存。
