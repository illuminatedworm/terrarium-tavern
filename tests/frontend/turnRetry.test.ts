import { expect, it } from 'vitest';
import { canRetryTurn } from '../../web/lib/turnRetry';

it('keeps failed input retryable after settings edits, but not after moving branches or a newer turn', () => {
  const turn = { id: 'failed', status: 'error', retry_head: 'greeting', request: { expected_revision: 0 } };
  const session = { revision: 2, head: 'greeting', turns: [{ id: 'failed' }] };
  expect(canRetryTurn(turn, session)).toBe(true);
  expect(canRetryTurn(turn, { ...session, head: 'another-branch' })).toBe(false);
  expect(canRetryTurn(turn, { ...session, turns: [{ id: 'newer' }] })).toBe(false);
  expect(canRetryTurn({ ...turn, request: { ...turn.request, scene_refresh: 'memory' } }, session)).toBe(
    false,
  );
});
