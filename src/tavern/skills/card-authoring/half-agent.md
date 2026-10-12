# Half Agent 制卡

制卡和 MVU 分流遵循共用 runtime-guide.md，无需因为使用 MVU 而先选择模式。Writer 无工具生成故事正文；Tracker 的 MVU 任务根据用户授权维护变量。稳定叙事设定默认 audience=writer，变量含义、更新条件和约束放在 extensions.terrarium.audience=tracker_mvu 条目；InitVar YAML 数据放 runtime_init。初始化由程序执行，启用的 tracker_mvu 条目在更新变量时全部提供给 Tracker。

变量工具采用相对于 stat_data 的 JSON Patch，无变化提交 []。卡片主要写清变量语义、触发条件、范围和约束，不让 Writer 在新回复中输出内联变量块。保留已有 schema 注册、校验回调、宏和资源。

适配已有卡前读取 read_skill(resource="SKILL.md") 和 references/workflow.md。普通润色不触发迁移，现有开场和内联初始化默认保留；初值仅放在世界书或开场原文，不另存隐藏副本。制作后检查实际初始化、正则样例和脚本启动；这些检查不等于真实模型的变量判断正确。
