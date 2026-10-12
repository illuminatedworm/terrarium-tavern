# 角色卡创作与字段

角色卡把可持续互动的设定交给游玩模型。人物需要动机、边界和说话方式；世界或情境卡需要可行动的入口、关系和规则。避免用一串形容词替代具体行为，也不要替用户预先完成剧情。

description 放不可缺少的核心设定，personality 补性格，scenario 说明当前互动背景。first_mes 是用户实际看到的起点，alternate_greetings 为独立可选开场。mes_example 展示语气与互动方式，不是过去已经发生的剧情。system_prompt/post_history_instructions 放确有必要的创作要求，避免互相矛盾或复制整个描述。creator_notes 是给读者的说明，不是模型必然收到的资料。

两种 Agent 模式均注入上述六类非空文本字段，分别包裹在 card_system_prompt、card_description、card_personality、card_scenario、card_mes_example、card_post_history_instructions 标签内。前五类放在固定资料区，后置要求放在当前输入之后；Half Agent 可按已保存配方调整。运行时只用标签标识字段来源，不另加用途说明或按字段名限制文本含义。只发送用户选中的开场，其余备选开场不作为剧情。卡内创作要求不替换运行规则或工具权限，运行必需信息不要只写入 creator_notes 或未知 extensions。

世界书把按需相关的背景、人物、地点和机制拆成完整条目。constant 表示常驻；keys/secondary_keys 是定位与准入线索；enabled 控制是否参与。不要为提高召回把所有条目设为常驻。数组与对象两种 entries 均需保留原形。新增条目省略内部 ID，程序负责分配。

新卡先说明人物/世界定位、用户角色、互动方式、风格、开场方向及必要机制，取得确认后再制作。可直接使用用户已完整给定的方案，不必重复确认已经授权的工作。创建时用 H 引用保存方案和确认原文；讨论尚未确认的内容不要升级为定案。

路径相对于原始卡片根，有 spec 的卡用 /data/...，旧卡用 /...。完整读取后再改，JSON Patch 的 add 要求父节点存在，数组追加用 /-。保留未知扩展、脚本、字面宏、作者资料和资源。现有 C 引用编辑后仍是同一卡的最新草稿。

世界书正文、标题、人物介绍等单个已有文本字段优先使用 set_text_field，提供 card_ref、path、完整 text 和 comment；由程序包装补丁，不手工构造数组或嵌套 JSON。开场使用 set_opening，结构修改或跨字段原子修改使用 patch_draft。当前上下文中版本仍有效的完整读取可跨轮复用；换窗、上下文重建或版本失效后重新读取 current，概览与搜索摘要不能代替完整读取。

普通文本卡不用增加 MVU、脚本或工作流元数据。需要 MVU 或脚本时读取共用 runtime-guide.md；MVU 设计无需先选择模式，只有特殊脚本能力影响方案时才确认兼容性目标。validate_draft 的完整性提示是创作检查，结构合法不能证明脚本可用。test_draft 只检查明确列出的本地行为；未覆盖的叙事质量由用户验收。

用户角色库与角色卡分开，只含名称和介绍。只有用户本轮明确要求相关操作时才使用 list_personas、read_persona、create_persona、update_persona，并引用本轮最新用户请求 H 编号。普通制卡、{{user}} 宏、方案里的玩家定位和助手建议都不授权访问或配套创建用户角色。只查看不写入；修改前完整读取，以 P 编号定位并保留未要求修改的字段。用户角色在本任务正常完成后直接保存，停止或失败可续接，已有故事的身份快照不随之改变。
