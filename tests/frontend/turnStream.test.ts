import { expect, it } from 'vitest';
import { eventCursor, requestScope } from '../../web/lib/turnStream';

it('does not append replayed stream events twice', () => {
  const accept = eventCursor();
  const events = [1, 2, 2, 1, 3].filter((seq) => accept({ seq, kind: 'text', payload: {} }));
  expect(events).toEqual([1, 2, 3]);
});

it('invalidates pending responses on every view transition', () => {
  const scope = requestScope(),
    first = scope.enter();
  expect(scope.owns(first)).toBe(true);
  const second = scope.enter();
  expect(scope.owns(first)).toBe(false);
  expect(scope.owns(second)).toBe(true);
});
