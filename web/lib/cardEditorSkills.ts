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
    description: '将模型每轮输出的状态栏，改为独立变量和项目变量面板。',
    scope: '处理 HTML、Markdown 表格或固定标签，同时迁移相关开场示范与初值。',
    outcome: '适合要求模型反复填写状态栏的卡。含义不明的数值会列为待确认项。',
    prompt:
      '请使用内置 skill 的正文状态栏迁移流程，将这张卡的正文状态栏改为独立变量，使用项目变量面板，同时处理相关开场示范和各开场初值。保留叙事设定，含义不明的字段先指出，不要编造计算规则。完成后验证并报告改动。',
  },
] as const;

export type EditorSkillPreset = (typeof editorSkillPresets)[number];

export function appendSkillRequest(input: string, cardRef: string, preset: EditorSkillPreset): string {
  return `${input}${input ? '\n\n' : ''}@${cardRef} ${preset.prompt}`;
}
