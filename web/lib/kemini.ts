import type { TurnEvent } from './turnStream';

export const keminiCompatibilityNotice =
  '此对话使用 Kemini 式实验规则：剧情请求保留消息顺序，消息适配完成后，中途 system 转为 user；开头连续的 system 与工具交换保留。实际角色见上下文预览。';

export interface KeminiSettings {
  enabled: boolean;
  normal_disclaimer_enabled: boolean;
  clear_prompt: string;
  identity_prompt: string;
  info_prompt: string;
  acknowledgement: string;
  thinking_prompt: string;
  filler_text: string;
  disclaimer_text: string;
  transport_prompt: string;
  tool_description: string;
  content_description: string;
}
export interface AuxiliaryPart {
  kind: 'thinking' | 'disclaimer' | 'extra';
  text: string;
}
export const auxiliaryLabels: Record<AuxiliaryPart['kind'], string> = {
  thinking: '显式思考',
  disclaimer: '免责声明',
  extra: '调用外文本',
};
export function latestAuxiliary(events: TurnEvent[]): AuxiliaryPart[] {
  for (let index = events.length - 1; index >= 0; index--)
    if (events[index].kind === 'kemini_auxiliary') return events[index].payload.items;
  return [];
}
