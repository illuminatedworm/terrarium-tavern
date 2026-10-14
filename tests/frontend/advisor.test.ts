import { describe, expect, it } from 'vitest';
import { advisorRunning, eventCursor, type AdvisorRun } from '../../web/lib/advisor';

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
});
