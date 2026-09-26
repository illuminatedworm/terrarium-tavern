import { expect, it } from 'vitest';
import { liveActivity, mergeActivity } from '../../web/lib/agentActivity';
import { mergeWorkTurns } from '../../web/lib/workRecords';

it('pairs streamed calls/results in order and ignores replay and foreign turns', () => {
  const event = (seq: number, kind: string, activity_id: string, order: number, more = {}) => ({
    seq,
    kind,
    turn: 'current',
    payload: { activity_id, order, name: 'read_history', arguments: '{"ref":"H2"}', ...more },
  });
  const first = event(1, 'full_agent_tool_started', 'call-1-0', 11);
  const events = [
    event(4, 'full_agent_tool', 'call-1-0', 11, { result: { text: '原文' } }),
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
          result: { error: '未找到笔记' },
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
