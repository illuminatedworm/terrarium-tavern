export interface EditorCard {
  ref: string;
  card: string | null;
  kind: 'reference' | 'draft';
  document: Record<string, any>;
  base_document: Record<string, any>;
  source: Record<string, any>;
  base_revision: number | null;
  target_mode: 'half_agent' | 'full_agent' | null;
  brief: string;
  source_exists?: boolean;
  source_changed?: boolean;
  can_undo?: boolean;
  validation?: { valid: boolean; errors: string[]; warnings: string[] };
}
export interface EditorRun {
  id: string;
  status: string;
  request: { text: string; resume_run_id?: string };
  response: string;
  error: string | null;
}
export interface EditorConversation {
  id: string;
  title: string;
  revision: number;
  cards: EditorCard[];
  state: { checkpoint?: string; window?: number };
  messages: { ref: string; role: string; content: string; run: string }[];
  runs: EditorRun[];
  pending: { run: string; cards: EditorCard[]; state: Record<string, any>; calls: number } | null;
}
export function cardName(card: EditorCard) {
  return (card.document.spec ? card.document.data : card.document)?.name || '未命名角色';
}
export function mentionCard(input: string, ref: string) {
  return /(?:^|\s)@[^\s@]*$/.test(input)
    ? input.replace(/@[^\s@]*$/, `@${ref} `)
    : `${input}${input && !/\s$/.test(input) ? ' ' : ''}@${ref} `;
}
