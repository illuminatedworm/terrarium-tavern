import type { TurnEvent } from './turnStream';

export interface KeminiSettings {
  enabled: boolean;
  normal_disclaimer_enabled: boolean;
  identity_prompt: string;
  acknowledgement: string;
  thinking_prompt: string;
  filler_text: string;
  disclaimer_text: string;
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
