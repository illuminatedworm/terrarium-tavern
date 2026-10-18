import { describe, expect, it } from 'vitest';
import { advisorOffers, advisorRunning, eventCursor, type AdvisorRun } from '../../web/lib/advisor';

describe('advisor observation', () => {
  it('deduplicates replayed SSE events without dropping the next event', () => {
    const cursor = eventCursor();
    const event = (seq: number) => ({ seq, kind: 'text', payload: { text: '片段' } });
    expect(cursor.accept(event(3))).toBe(true);
    expect(cursor.accept(event(3))).toBe(false);
    expect(cursor.accept(event(2))).toBe(false);
    expect(cursor.accept(event(4))).toBe(true);
    expect(eventCursor().accept(event(1))).toBe(true);
  });
  it('does not treat a recoverable stopped consultation as running', () => {
    expect(advisorRunning({ status: 'running' } as AdvisorRun)).toBe(true);
    expect(advisorRunning({ status: 'queued' } as AdvisorRun)).toBe(true);
    expect(advisorRunning({ status: 'error', resumable: true } as AdvisorRun)).toBe(false);
    expect(advisorRunning(null)).toBe(false);
  });
  it('shows offers only from calls the runtime accepted', () => {
    const call = (id: string, name: string, args: object) => ({
      role: 'assistant',
      content: '',
      tool_calls: [{ id, function: { name, arguments: JSON.stringify(args) } }],
    });
    const result = (id: string, body: object) => ({
      role: 'tool',
      tool_call_id: id,
      content: JSON.stringify(body),
    });
    expect(
      advisorOffers([
        call('a', 'offer_inputs', { options: ['推门进去'] }),
        result('a', { shown: true }),
        call('b', 'offer_titles', { titles: ['雨夜'], request_ref: 'Q2' }),
        result('b', { error: '参数无效' }),
      ]),
    ).toEqual({ inputs: ['推门进去'], titles: [] });
  });
});
