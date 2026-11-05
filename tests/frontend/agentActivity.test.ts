import { expect, it, vi } from 'vitest';
import { liveActivity, mergeActivity } from '../../web/lib/agentActivity';
import { mergeWorkTurns } from '../../web/lib/workRecords';
import { appendVisualInput, hasVisuals, loadVisuals, observeVisuals } from '../../web/lib/visuals';
import { rawApi } from '../../web/lib/api';

vi.mock('../../web/lib/api', () => ({ rawApi: vi.fn() }));

it('batches visible graphics across callback microtasks and preserves unsent drafts', async () => {
  const frames: FrameRequestCallback[] = [];
  const callbacks: Array<(entries: Array<{ target: Element; isIntersecting: boolean }>) => void> = [];
  class Observer {
    constructor(callback: (entries: Array<{ target: Element; isIntersecting: boolean }>) => void) {
      callbacks.push(callback);
    }
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  vi.stubGlobal('IntersectionObserver', Observer);
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => frames.push(callback));
  vi.mocked(rawApi).mockResolvedValue({ messages: { a: [], b: [] } });
  const first = {} as Element,
    second = {} as Element;
  const requests: ReturnType<typeof loadVisuals>[] = [];
  const stopFirst = observeVisuals(first, () => {
    requests.push(loadVisuals('story', 'a'));
  });
  const stopSecond = observeVisuals(second, () => {
    requests.push(loadVisuals('story', 'b'));
  });
  try {
    expect(callbacks).toHaveLength(1);
    callbacks[0]([{ target: first, isIntersecting: true }]);
    await Promise.resolve(); // A browser can run microtasks before notifying the next element.
    callbacks[0]([{ target: second, isIntersecting: true }]);
    await Promise.resolve();
    expect(rawApi).not.toHaveBeenCalled();
    expect(frames).toHaveLength(1);
    frames[0](0);
    await Promise.all(requests);
    expect(rawApi).toHaveBeenCalledExactlyOnceWith('/sessions/story/full-agent/visuals', {
      message_ids: ['a', 'b'],
    });
    const returned = {
      id: 'v',
      order: 1,
      kind: 'tool' as const,
      name: 'present_ui',
      status: 'returned' as const,
      summary: '',
    };
    expect(hasVisuals([returned])).toBe(true);
    expect(hasVisuals([returned], true)).toBe(false);
    expect(hasVisuals([{ ...returned, status: 'error' }])).toBe(false);
    expect(hasVisuals()).toBe(false);
    const draft = '我还没写完的内容  \n\n';
    expect(appendVisualInput(draft, '查看庭院')).toBe(draft + '查看庭院');
    expect(appendVisualInput('已有草稿', '查看庭院')).toBe('已有草稿\n查看庭院');
    expect(appendVisualInput('', '查看庭院')).toBe('查看庭院');
  } finally {
    stopFirst();
    stopSecond();
    vi.unstubAllGlobals();
    vi.mocked(rawApi).mockReset();
  }
});

it('hides future outline spoilers from collapsed live activity summaries', () => {
  const [item] = liveActivity(
    [
      {
        seq: 1,
        turn: 't',
        kind: 'full_agent_tool_started',
        payload: {
          activity_id: 'plot',
          name: 'write_plot',
          arguments: '{"content":"SPOILER_OUTLINE"}',
        },
      },
    ],
    't',
    'running',
  );
  expect(item.summary).toBe('包含未来剧情，展开查看');
  expect(item.details?.arguments).toContain('SPOILER_OUTLINE');
});

it('pairs streamed calls/results in order and ignores replay and foreign turns', () => {
  const event = (seq: number, kind: string, activity_id: string, order: number, more = {}) => ({
    seq,
    kind,
    turn: 'current',
    payload: { activity_id, order, name: 'read_history', arguments: '{"ref":"H2"}', ...more },
  });
  const first = event(1, 'full_agent_tool_started', 'call-1-0', 11);
  const events = [
    event(4, 'full_agent_tool', 'call-1-0', 11, { result: { ok: true, data: { text: '原文' } } }),
    first,
    first,
    event(3, 'full_agent_tool_started', 'call-1-1', 12),
    { ...event(5, 'full_agent_tool', 'foreign', 1), turn: 'other' },
  ];
  const items = liveActivity(events, 'current', 'running');
  expect(items.map((item) => item.id)).toEqual(['call-1-0', 'call-1-1']);
  expect(items.map((item) => item.status)).toEqual(['returned', 'running']);
  expect(items[0].details?.result).toContain('原文');
  expect(liveActivity(events, 'current', 'cancelled')[1].status).toBe('interrupted');
  expect(mergeActivity(items, items)).toHaveLength(2);
  const snapshot = { ...items[0], event_seq: 20, status: 'returned' as const };
  expect(mergeActivity([snapshot], liveActivity([first], 'current', 'running'))[0].status).toBe('returned');
  const received = {
    seq: 6,
    turn: 'current',
    kind: 'full_agent_runtime',
    payload: {
      id: 'runtime-3',
      order: 30,
      name: 'novel_report_delivery',
      summary: 'R1 · 原著调查报告已送达',
      status: 'running',
      reasoning_content: 'PRIVATE_REASONING',
    },
  };
  const dispatched = {
    ...received,
    seq: 7,
    payload: { ...received.payload, status: 'returned' },
  };
  const runtime = liveActivity([received, dispatched, received], 'current', 'running');
  expect(runtime).toMatchObject([{ id: 'runtime-3', kind: 'runtime', status: 'returned' }]);
  expect(JSON.stringify(runtime)).not.toContain('PRIVATE_REASONING');
  expect(liveActivity([received], 'current', 'cancelled')[0].status).toBe('interrupted');
  expect(mergeActivity(runtime, liveActivity([received], 'current', 'running'))[0].status).toBe('returned');
});

it('keeps errors visible and hides transport metadata from dialogue activity', () => {
  const [item] = liveActivity(
    [
      {
        seq: 1,
        turn: 't',
        kind: 'full_agent_tool',
        payload: {
          activity_id: 'a',
          order: 1,
          name: 'read_note',
          arguments: '{"key":"约定"}',
          result: {
            ok: false,
            error: {
              code: 'INVALID_REFERENCE',
              message: '未找到笔记',
              retryable: true,
              next_action: '重新搜索笔记',
            },
          },
          reasoning_content: 'PRIVATE_REASONING',
        },
      },
    ],
    't',
    'error',
  );
  expect(item.status).toBe('error');
  expect(item.summary).toBe('约定');
  expect(JSON.stringify(item)).not.toContain('PRIVATE_REASONING');
});

it('prepends older debug pages, appends new turns and updates existing statuses', () => {
  const initial = mergeWorkTurns(
    [],
    [
      { id: 'd', created: 4 },
      { id: 'c', created: 3 },
    ],
  );
  const older = mergeWorkTurns(initial, [
    { id: 'b', created: 2 },
    { id: 'a', created: 1 },
  ]);
  const updated = mergeWorkTurns(older, [
    { id: 'e', created: 5 },
    { id: 'd', created: 4, status: 'committed' },
  ]);
  expect(updated.map((turn) => turn.id)).toEqual(['a', 'b', 'c', 'd', 'e']);
  expect(updated[3].status).toBe('committed');
});
