export const sources = [
  {
    value: 'rules',
    label: '应用系统提示词',
    description:
      '注入“设置 → 系统提示词”中的全局指令，用于指导角色扮演和回复。对话级“本轮偏好”由 Roleplay Runtime 另行补充，不由这个块控制。',
  },
  {
    value: 'lore_before',
    label: '世界书 · 角色设定前',
    description:
      '注入当前对话有效世界书中已触发、配置为角色设定前的条目正文。不是全部世界书，也不是 Agent 的历史摘要。',
  },
  {
    value: 'character',
    label: '角色设定（合并）',
    description:
      '兼容旧配方：合并注入角色卡的系统提示、描述、性格和场景，各字段分别加标签。可拆成独立内容块调整位置。',
  },
  {
    value: 'system_prompt',
    label: '角色卡系统提示',
    description: '注入角色卡的 system_prompt 字段，以 <card_system_prompt> 包裹；与应用系统提示词独立。',
  },
  {
    value: 'description',
    label: '角色卡描述',
    description: '注入角色卡的 description 字段，以 <card_description> 包裹，可包含人物或世界设定。',
  },
  {
    value: 'personality',
    label: '角色卡性格',
    description: '注入角色卡的 personality 字段，以 <card_personality> 包裹。',
  },
  {
    value: 'scenario',
    label: '角色卡场景',
    description: '注入角色卡的 scenario 字段，以 <card_scenario> 包裹。',
  },
  {
    value: 'persona',
    label: '用户身份',
    description: '注入会话设置中的用户名字与完整描述，帮助角色理解用户的身份和背景。',
  },
  {
    value: 'lore_after',
    label: '世界书 · 角色设定后',
    description: '注入已触发、配置为角色设定后的世界书条目正文。配方顺序决定该内容块与其他块的相对位置。',
  },
  {
    value: 'examples',
    label: '示例对话',
    description: '注入角色卡中的示例对话，提供口吻和互动风格参考；它不是已经发生的剧情。',
  },
  {
    value: 'memory',
    label: '对话记忆',
    description:
      '注入后台 Tracker 整理的剧情记忆和重要来源记录。历史窗口已经裁剪时，记忆会作为必需内容保留，即使关闭这个块也会单独注入。',
  },
  {
    value: 'history',
    label: '剧情消息历史',
    description:
      '注入当前进度中尚未退出历史窗口的历史消息，逐条保留原来的用户 / AI 角色。锚定在这些消息前的滚动世界书随消息一起注入或退出。这个块的消息角色、深度、超长提示阈值和保留标记不控制历史窗口。',
  },
  {
    value: 'author_note',
    label: '作者提示',
    description: '注入已触发、配置为作者提示的世界书条目。可用于剧情节奏、氛围或其他引导。',
  },
  {
    value: 'state',
    label: '当前状态与变量',
    description: '注入当前进度的剧情状态，以文字和 JSON 变量数据提供给模型；不是右侧栏渲染面板的画面。',
  },
  {
    value: 'input',
    label: '本次用户输入',
    description: '注入本次发送的用户文字，通常使用 user 角色。拖动这个块会改变输入在配方中的位置。',
  },
  {
    value: 'post',
    label: '历史后提示词',
    description: '注入角色卡的历史后提示词，用于在剧情历史之后补充回复要求。实际顺序由配方或深度决定。',
  },
  {
    value: 'custom',
    label: '自定义文本',
    description:
      '注入下方填写的文本。可使用 {{char}}、{{user}} 和状态路径宏。其他来源标识会先查找Roleplay Runtime 动态来源，找不到时使用此处的自定义文本。',
  },
];
