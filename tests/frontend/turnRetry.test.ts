import { expect, it } from 'vitest';
import { canRetryTurn, createTurnContinuation, resumeBlocker } from '../../web/lib/turnRetry';

it('keeps failed input retryable after settings edits, but not after moving branches or a newer turn', () => {
  const turn = { id: 'failed', status: 'error', retry_head: 'greeting', request: { expected_revision: 0 } };
  const session = { revision: 2, head: 'greeting', turns: [{ id: 'failed' }] };
  expect(canRetryTurn(turn, session)).toBe(true);
  expect(canRetryTurn(turn, { ...session, head: 'another-branch' })).toBe(false);
  expect(canRetryTurn(turn, { ...session, turns: [{ id: 'newer' }] })).toBe(false);
  expect(canRetryTurn({ ...turn, request: { ...turn.request, scene_refresh: 'memory' } }, session)).toBe(
    false,
  );
  const resumable = {
    resumable: true,
    request: { operation_id: 'original', expected_revision: 2, allow_content_edit: true },
  };
  const current = { ...session, agent_mode: 'full_agent' as const };
  // Changed authorization or settings block continuing with a visible reason,
  // never by quietly turning the continuation into a fresh start.
  expect(resumeBlocker(resumable, current, { allow_content_edit: true })).toBeNull();
  expect(resumeBlocker(resumable, { ...current, revision: 3 }, { allow_content_edit: true })).toContain(
    '会话设置已变更',
  );
  expect(resumeBlocker(resumable, current, { allow_content_edit: false })).toContain('授权已改变');
});

it('releases the next input once after its matching commit, retaining review and blocking failed or stale work', () => {
  const continuation = createTurnContinuation();
  continuation.start('resumed', 1);
  expect(continuation.finish({ id: 'resumed', status: 'awaiting_review' }, 1)).toBe(false);
  expect(continuation.pending.value).toEqual({ turnId: 'resumed', view: 1 });
  expect(continuation.finish({ id: 'another', status: 'committed' }, 1)).toBe(false);
  expect(continuation.finish({ id: 'resumed', status: 'committed' }, 1)).toBe(true);
  expect(continuation.finish({ id: 'resumed', status: 'committed' }, 1)).toBe(false);
  expect(continuation.pending.value).toBeNull();
  continuation.start('failed', 1);
  expect(continuation.finish({ id: 'failed', status: 'error' }, 1)).toBe(false);
  expect(continuation.pending.value).toBeNull();
  continuation.start('cancelled', 1);
  continuation.cancel();
  expect(continuation.finish({ id: 'cancelled', status: 'committed' }, 1)).toBe(false);
  continuation.start('new-view', 2);
  expect(continuation.finish({ id: 'new-view', status: 'committed' }, 1)).toBe(false);
  expect(continuation.pending.value).toEqual({ turnId: 'new-view', view: 2 });
});
