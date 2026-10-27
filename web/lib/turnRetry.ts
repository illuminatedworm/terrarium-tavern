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
export function canResumeTurn(
  turn: Pick<TurnRecord, 'request' | 'resumable'>,
  session: Pick<Session, 'agent_mode' | 'revision'>,
  options: Pick<TurnRequest, 'allow_content_edit' | 'update_variables' | 'regenerate_include_reply'>,
): boolean {
  return (
    session.agent_mode === 'full_agent' &&
    turn.resumable &&
    turn.request.expected_revision === session.revision &&
    !!options.allow_content_edit === !!turn.request.allow_content_edit &&
    !!options.update_variables === !!turn.request.update_variables &&
    !!options.regenerate_include_reply === !!turn.request.regenerate_include_reply
  );
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
