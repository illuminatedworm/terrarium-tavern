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
