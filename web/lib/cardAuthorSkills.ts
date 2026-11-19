export const editorSkillPresets = [
  {
    id: 'adapt',
    title: '适配项目工作流',
    description: '让导入的角色卡按本项目的叙事、变量与初始化方式工作。',
    scope: '检查世界书、提示词和脚本，分离设定与变量规则，保留原有开场。',
    outcome: '适合从其他酒馆导入的角色卡。完成后提供草稿差异和验证结果。',
  },
  {
    id: 'status-display',
    title: '迁移正文状态栏',
    description: '将模型每轮填写的状态栏，改为独立变量驱动的原样面板。',
    scope: '保留原面板设计，迁移相关开场示范与初值，检查状态变化后的显示。',
    outcome: '适合要求模型反复填写状态栏的卡。含义不明的数值会列为待确认项。',
  },
] as const;

export type EditorSkillPreset = (typeof editorSkillPresets)[number];

export type EditorSkillId = EditorSkillPreset['id'];
