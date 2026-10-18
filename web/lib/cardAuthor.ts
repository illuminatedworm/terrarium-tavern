import { compare } from 'fast-json-patch';

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
  portrait_changed?: boolean;
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
export interface EditorMessage {
  attachments?: EditorFile[];
  ref: string;
  role: string;
  content: string;
  run: string;
}
export interface EditorFile {
  ref: string;
  name: string;
  size_bytes: number;
  extension: string;
  message_ref?: string | null;
  mime_type?: string;
  width?: number;
  height?: number;
}
export interface EditorConversation {
  files?: EditorFile[];
  channel_origin?: 'standard' | 'chatgpt' | null;
  model_selection?: { provider_id: string; model: string; provider_models?: Record<string, string> };
  id: string;
  title: string;
  revision: number;
  cards: EditorCard[];
  state: { handoff?: string; window?: number; persona_attachments?: Record<string, EditorPersona> };
  messages: EditorMessage[];
  runs: EditorRun[];
  pending: { run: string; cards: EditorCard[]; state: Record<string, any>; calls: number } | null;
}

export interface EditorPersona {
  id: string;
  name: string;
  description: string;
  version: number;
}

export type EditorTimelineItem =
  | { kind: 'message'; key: string; message: EditorMessage }
  | { kind: 'activity'; key: string; run: EditorRun };

/** Attach a run to its answer, or its chronological gap if no answer was committed.
 * Resuming without new text creates a run with no user message; it must still
 * stay between the preceding attempt and the next exchange, including on reload.
 */
export function editorTimeline(messages: EditorMessage[], runs: EditorRun[]): EditorTimelineItem[] {
  const positions = new Map<string, number[]>();
  messages.forEach((message, index) => {
    const indexes = positions.get(message.run) || [];
    indexes.push(index);
    positions.set(message.run, indexes);
  });
  const insertions = new Map<number, EditorRun[]>();
  runs.forEach((run, index) => {
    const indexes = positions.get(run.id) || [];
    const answer = indexes.find((i) => messages[i].role === 'assistant');
    const nextRun = runs.slice(index + 1).find((next) => positions.has(next.id));
    const position =
      answer ??
      (indexes.length ? indexes.at(-1)! + 1 : nextRun ? positions.get(nextRun.id)![0] : messages.length);
    insertions.set(position, [...(insertions.get(position) || []), run]);
  });
  const timeline: EditorTimelineItem[] = [];
  for (let index = 0; index <= messages.length; index++) {
    for (const run of insertions.get(index) || [])
      timeline.push({ kind: 'activity', key: `run:${run.id}`, run });
    if (index < messages.length)
      timeline.push({ kind: 'message', key: `message:${messages[index].ref}`, message: messages[index] });
  }
  return timeline;
}
export function cardName(card: EditorCard) {
  return (card.document.spec ? card.document.data : card.document)?.name || '未命名角色';
}

export function draftReview(card: EditorCard | undefined, pending = false) {
  const changes = card ? compare(card.base_document, card.document) : [];
  const count = changes.length + Number(!!card?.portrait_changed);
  const status = pending
    ? '本轮未完成版本'
    : count
      ? '独立草稿 · 有未保存修改'
      : card?.kind === 'reference'
        ? '引用快照 · 尚未修改'
        : card?.card
          ? '与已保存版本一致'
          : '独立草稿 · 尚未保存';
  return { changes, count, status };
}
