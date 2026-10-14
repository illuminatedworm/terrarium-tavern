import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useAdvisor } from '../../web/composables/useAdvisor';
import { rawApi } from '../../web/lib/api';
import type { AdvisorState } from '../../web/lib/advisor';

vi.mock('../../web/lib/api', () => ({ rawApi: vi.fn() }));
vi.mock('vue', async (importOriginal) => ({
  ...(await importOriginal<typeof import('vue')>()),
  onBeforeUnmount: vi.fn(),
}));

const snapshot = (revision: number, text: string) =>
  ({ revision, run: null, exchanges: text ? [{ user_text: text }] : [], context: {} }) as AdvisorState;

beforeEach(() => vi.mocked(rawApi).mockReset());

describe('clearing the current consultation', () => {
  it('ignores a pre-clear load that arrives after the cleared state', async () => {
    const advisor = useAdvisor('story', vi.fn());
    advisor.state.value = snapshot(1, 'old');
    let finishLoad!: (value: AdvisorState) => void;
    vi.mocked(rawApi)
      .mockImplementationOnce(() => new Promise((resolve) => (finishLoad = resolve)))
      .mockResolvedValueOnce(snapshot(2, ''));
    const loading = advisor.load(false);
    advisor.streaming.value = 'old stream';
    advisor.notice.value = 'old notice';
    advisor.error.value = 'old error';
    expect(await advisor.clear()).toBe(true);
    finishLoad(snapshot(1, 'old'));
    await loading;
    expect(advisor.state.value?.revision).toBe(2);
    expect(advisor.state.value?.exchanges).toEqual([]);
    expect(advisor.streaming.value + advisor.notice.value + advisor.error.value).toBe('');
  });

  it('locks sending while clearing and preserves the conversation if clearing fails', async () => {
    const advisor = useAdvisor('story', vi.fn());
    advisor.state.value = snapshot(1, 'keep');
    let reject!: (error: Error) => void;
    vi.mocked(rawApi).mockImplementationOnce(() => new Promise((_, fail) => (reject = fail)));
    const clearing = advisor.clear();
    expect(advisor.running.value).toBe(true);
    expect(await advisor.send('race')).toBe(false);
    reject(new Error('revision conflict'));
    expect(await clearing).toBe(false);
    expect(advisor.state.value?.exchanges[0].user_text).toBe('keep');
    expect(advisor.error.value).toContain('revision conflict');
    expect(advisor.running.value).toBe(false);
  });
});
