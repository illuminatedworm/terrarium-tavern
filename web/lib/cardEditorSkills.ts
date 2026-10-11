/** User-facing entry points for the bundled adapt-card-to-tavern skill.
 * These compose an ordinary editable request; they never invoke a run directly.
 */
export const editorSkillPresets = [
  {
    id: 'adapt',
    title: '适配项目工作流',
    description: '让导入的角色卡按本项目的叙事、变量与初始化方式工作。',
    scope: '检查世界书、提示词和脚本，分离设定与变量规则，保留原有开场。',
    outcome: '适合从其他酒馆导入的角色卡。完成后提供草稿差异和验证结果。',
    prompt:
      '请使用内置的角色卡适配 skill，将这张卡适配本项目工作流。分离叙事设定、变量规则与初始化，保留开场和无关设定，检查草稿并说明未解决的问题。',
  },
  {
    id: 'status-display',
    title: '迁移正文状态栏',
    description: '将模型每轮填写的状态栏，改为独立变量驱动的原样面板。',
    scope: '保留原面板设计，迁移相关开场示范与初值，检查状态变化后的显示。',
    outcome: '适合要求模型反复填写状态栏的卡。含义不明的数值会列为待确认项。',
    prompt:
      '请使用内置 skill 的正文状态栏迁移流程，将这张卡的正文状态栏改为独立变量，保留原面板视觉设计并改为读取变量的展示前端，挂载到项目 MVU 面板，同时处理相关开场示范和各开场初值。保留叙事设定，以及已正确初始化并对模型隐藏的开场特殊数据和对应规则；沿用有效初值来源，不强制抽离内联 MVU。含义不明的字段先指出，不要编造计算规则。完成后验证初始化、开场与后续回复的前端显示，以及变量更新后的显示，并报告改动。',
  },
] as const;

export type EditorSkillPreset = (typeof editorSkillPresets)[number];

export function appendSkillRequest(input: string, name: string, preset: EditorSkillPreset): string {
  return `${input}${input ? '\n\n' : ''}关于角色卡「${name}」：${preset.prompt}`;
}
