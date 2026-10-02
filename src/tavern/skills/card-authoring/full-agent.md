# Full Agent 制卡

制卡和 MVU 分流遵循共用 runtime-guide.md，无需因为使用 MVU 而先选择模式。Full Agent 在同一工具循环中叙事、检索设定、维护笔记并按授权 patch_state 更新 MVU。当前有效上下文首次授权变量更新时，自动追加全部启用的 tracker_mvu 条目；同版本不重复，换窗或重建后按需补入。

变量规则写清名称、含义、变化条件、范围和约束，使用 tracker_mvu；稳定叙事设定保留 writer，初始化数据使用 runtime_init 或已支持的 InitVar。规则及宏值按轮固定，本轮修改下轮生效。工具补丁路径相对于 stat_data，无变化提交 []。无需复制工具 schema，也不要要求模型输出 UpdateVariable/JSONPatch 正文区块。

Full Agent 不支持脚本额外发起模型请求和 HTML 交互生成。保留受支持的 schema/校验逻辑，遇到这些不支持能力应说明并与用户决定替代交互，不能通过静态验证就声称完整适配。

适配流程共用 SKILL.md 和 references/workflow.md，已有 tracker_mvu 规则可直接使用，不必迁往 writer。开场和初始化关系仍由 set_opening 管理，普通文本修改不自动迁移。使用 test_draft 检查所选开场本地行为，真实叙事和更新判断另行验收。
