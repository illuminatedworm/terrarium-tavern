import { expect, it } from 'vitest';
import {
  DIRECT_MESSAGE_LIMIT,
  DIRECT_TEXT_LIMIT,
  HistoryHeights,
  historyMessageIntervals,
  historyRefLabel,
  historyRounds,
  historySummary,
  needsVirtualHistory,
} from '../../web/lib/storyHistory';

const messages = (count: number) =>
  Array.from({ length: count }, (_, index) => ({ id: String(index), role: 'user', raw: '正文' }));

it.each([
  [0, '+0分0秒'],
  [1.9, '+0分1秒'],
  [61, '+1分1秒'],
  [3661, '+61分1秒'],
  [86400, '+1440分0秒'],
  [86400.1, undefined],
  [-1, undefined],
])('formats a %s second message gap with the 24-hour cutoff', (elapsed, expected) => {
  const base = 1_790_000_000;
  const intervals = historyMessageIntervals([
    { id: 'first', role: 'user', created: base },
    { id: 'reply', role: 'assistant', created: base + Number(elapsed) },
  ]);
  expect(intervals.has('first')).toBe(false);
  expect(intervals.get('reply')).toBe(expected);
});

it('uses the immediately preceding message and hides gaps when either timestamp is unavailable', () => {
  const history = [
    { id: 'greeting', role: 'assistant', created: 100 },
    { id: 'user', role: 'user', created: 165 },
    { id: 'reply', role: 'assistant', created: 310 },
    { id: 'missing', role: 'user' },
    { id: 'after-missing', role: 'assistant', created: 500 },
    { id: 'invalid', role: 'user', created: NaN },
    { id: 'after-invalid', role: 'assistant', created: 600 },
    { id: 'infinite', role: 'user', created: Infinity },
  ];
  expect([...historyMessageIntervals(history)]).toEqual([
    ['user', '+1分5秒'],
    ['reply', '+2分25秒'],
  ]);
  expect(historyMessageIntervals([]).size).toBe(0);
});

it('uses direct rendering at both limits and virtualizes only beyond either limit', () => {
  expect(needsVirtualHistory(messages(DIRECT_MESSAGE_LIMIT))).toBe(false);
  expect(needsVirtualHistory(messages(DIRECT_MESSAGE_LIMIT + 1))).toBe(true);
  expect(needsVirtualHistory([{ id: 'one', role: 'assistant', raw: '字'.repeat(DIRECT_TEXT_LIMIT) }])).toBe(
    false,
  );
  expect(
    needsVirtualHistory([{ id: 'one', role: 'assistant', raw: '字'.repeat(DIRECT_TEXT_LIMIT + 1) }]),
  ).toBe(true);
});

it('groups completed exchanges and leaves greeting and pending input navigable', () => {
  const history = [
    { id: 'greeting', role: 'assistant', raw: '开场' },
    { id: 'user', role: 'user', raw: '输入' },
    { id: 'reply', role: 'assistant', parent: 'user', raw: '回答' },
    { id: 'pending', role: 'user', raw: '新输入' },
  ];
  const rounds = historyRounds(history);
  expect(rounds.map((round) => round.messageIds)).toEqual([['greeting'], ['user', 'reply'], ['pending']]);
  expect(rounds[1].reply?.raw).toBe('回答');
  expect(
    historySummary({ id: 'html', role: 'assistant', raw: '<script>secret()</script><b>正文</b>\n内容' }),
  ).toBe('正文 内容');
});

it('labels sources with backend H refs in branch order and counts unknown sources', () => {
  expect(historyRefLabel(['H52', 'H52'])).toBe('H52');
  expect(historyRefLabel(['H52', undefined, 'H9'])).toBe('H9、H52');
  expect(historyRefLabel(['H12', 'H3', 'H100'])).toBe('H3 等 3 条');
  expect(historyRefLabel([undefined, undefined])).toBe('2 处');
});

it('locates 10000 variable height messages after repeated measurements', () => {
  const values = Array.from({ length: 10000 }, (_, index) => 80 + (index % 300));
  const heights = new HistoryHeights();
  heights.reset(values);
  for (const index of [0, 17, 298, 4000, 9999]) {
    values[index] += 271;
    heights.set(index, values[index]);
  }
  let sum = 0;
  values.forEach((height, index) => {
    expect(heights.offset(index)).toBe(sum);
    expect(heights.indexAt(sum + height / 2)).toBe(index);
    sum += height;
  });
  expect(heights.offset(values.length)).toBe(sum);
  expect(heights.indexAt(sum + 100)).toBe(9999);
  heights.reset([]);
  expect(heights.offset(0)).toBe(0);
});
