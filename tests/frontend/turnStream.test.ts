import { afterEach, expect, it, vi } from 'vitest';
import { eventCursor, observeTurn, requestScope, turnDraft, type TurnEvent } from '../../web/lib/turnStream';

afterEach(() => vi.unstubAllGlobals());

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

it('replays tool preambles, rejected previews and terminal replacements without concatenating them', () => {
  const events: TurnEvent[] = [
    { seq: 1, kind: 'text', payload: { text: '先查找资料' } },
    { seq: 2, kind: 'text_reset', payload: { text: '' } },
    { seq: 3, kind: 'full_agent_tool', payload: { name: 'read_state' } },
    { seq: 4, kind: 'text', payload: { text: '过早的正文' } },
    { seq: 5, kind: 'text_reset', payload: { text: '' } },
    { seq: 6, kind: 'text', payload: { text: '第一段' } },
    { seq: 7, kind: 'text', payload: { text: '第二段' } },
    { seq: 8, kind: 'text_reset', payload: { text: '脚本处理后的完整正文' } },
  ];
  const accept = eventCursor();
  const reconnect = [...events.slice(0, 6), ...events.slice(3)];
  expect(reconnect.filter(accept).reduce(turnDraft, '')).toBe('脚本处理后的完整正文');
  expect(events.reduce(turnDraft, '')).toBe('脚本处理后的完整正文');
});

it('ignores stale observer chunks and terminal events after switching sessions', () => {
  class Source {
    onmessage?: (event: { data: string }) => void;
    end?: () => void;
    close = vi.fn();
    addEventListener(_name: string, handler: () => void) {
      this.end = handler;
    }
  }
  vi.stubGlobal('EventSource', Source);
  const scope = requestScope();
  const token = scope.enter();
  let draft = '';
  const end = vi.fn(async () => {});
  const stream = observeTurn(
    'first',
    () => scope.owns(token),
    (event) => {
      draft = turnDraft(draft, event);
    },
    end,
  ) as unknown as Source;
  const chunk = { data: JSON.stringify({ seq: 1, kind: 'text', payload: { text: '临时正文' } }) };
  stream.onmessage!(chunk);
  stream.onmessage!(chunk);
  expect(draft).toBe('临时正文');
  scope.enter();
  stream.onmessage!({ data: JSON.stringify({ seq: 2, kind: 'text_reset', payload: { text: '' } }) });
  stream.end!();
  expect(draft).toBe('临时正文');
  expect(stream.close).toHaveBeenCalledOnce();
  expect(end).not.toHaveBeenCalled();
});
