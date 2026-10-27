import { ref } from 'vue';
import type { Session, TurnRecord, TurnRequest } from './types';

interface RetryTurn {
  id: string;
  status: string;
  retry_head?: string | null;
  request?: { expected_revision: number; scene_refresh?: unknown };
}
interface RetrySession {
  revision: number;
  head: string | null;
  turns: Array<{ id: string }>;
}
export function canRetryTurn(turn: RetryTurn | null, session: RetrySession | null): boolean {
  return !!(
    turn &&
    session &&
    ['error', 'cancelled', 'interrupted'].includes(turn.status) &&
    !turn.request?.scene_refresh &&
    session.turns[0]?.id === turn.id &&
    (turn.request?.expected_revision === session.revision ||
      (turn.retry_head != null && turn.retry_head === session.head))
  );
}
/**
 * Why a failed turn cannot continue its saved Full Agent progress; null when it can.
 * Continuing is only ever an explicit choice, so the page shows this reason
 * instead of silently starting the turn over.
 */
export function resumeBlocker(
  turn: Pick<TurnRecord, 'request' | 'resumable'>,
  session: Pick<Session, 'agent_mode' | 'revision'>,
  options: Pick<TurnRequest, 'allow_content_edit' | 'update_variables' | 'regenerate_include_reply'>,
): string | null {
  if (session.agent_mode !== 'full_agent' || !turn.resumable) return '本轮没有可继续的进度';
  if (turn.request.expected_revision !== session.revision) return '会话设置已变更，只能重新开始本轮';
  if (
    !!options.allow_content_edit !== !!turn.request.allow_content_edit ||
    !!options.update_variables !== !!turn.request.update_variables
  )
    return '本轮的设定或变量授权已改变，只能重新开始本轮';
  if (!!options.regenerate_include_reply !== !!turn.request.regenerate_include_reply)
    return '“带上原 AI 回复”选项已改变，只能重新开始本轮';
  return null;
}

/** The composer stays the only source of the next input, including edits during review. */
export function createTurnContinuation() {
  const pending = ref<{ turnId: string; view: number } | null>(null);
  return {
    pending,
    start(turnId: string, view: number) {
      pending.value = { turnId, view };
    },
    finish(turn: Pick<TurnRecord, 'id' | 'status'>, view: number): boolean {
      if (pending.value?.turnId !== turn.id || pending.value.view !== view) return false;
      if (turn.status === 'awaiting_review') return false;
      pending.value = null;
      return turn.status === 'committed';
    },
    cancel() {
      pending.value = null;
    },
  };
}
