import { effectScope, nextTick, reactive } from 'vue';
import { describe, expect, it, vi } from 'vitest';
import { api } from '../../web/lib/api';
import { useApiReasoning } from '../../web/lib/apiReasoning';

vi.mock('../../web/lib/api', () => ({ api: vi.fn() }));

describe('ordinary API reasoning capabilities', () => {
  it('never shows stale protocol options when requests finish out of order', async () => {
    const deferred: Record<string, (value: any) => void> = {};
    vi.mocked(api).mockImplementation((path: string) => new Promise((resolve) => (deferred[path] = resolve)));
    const profile = reactive({ api_mode: 'anthropic', model: 'switching-model' });
    const state = reactive({ enabled: true });
    const scope = effectScope();
    const result = scope.run(() =>
      useApiReasoning(
        () => profile,
        () => state.enabled,
      ),
    )!;
    const first = '/settings/reasoning?api_mode=anthropic&model=switching-model';
    const second = '/settings/reasoning?api_mode=gemini&model=switching-model';
    profile.api_mode = 'gemini';
    await nextTick();
    expect(result.details.value.supported_reasoning_levels).toEqual([]);
    deferred[second]({
      id: profile.model,
      display_name: profile.model,
      supported_reasoning_levels: [{ effort: 'low', description: '' }],
      note: 'Gemini',
    });
    await nextTick();
    deferred[first]({
      id: profile.model,
      display_name: profile.model,
      supported_reasoning_levels: [{ effort: 'max', description: '' }],
      note: 'Anthropic',
    });
    await nextTick();
    expect(result.details.value.supported_reasoning_levels?.map((level) => level.effort)).toEqual(['low']);
    expect(result.note.value).toBe('Gemini');
    state.enabled = false;
    await nextTick();
    expect(result.details.value.supported_reasoning_levels).toEqual([]);
    scope.stop();
  });

  it('reports failures, keeps other protocols usable and retries on return', async () => {
    vi.mocked(api)
      .mockRejectedValueOnce(new Error('local endpoint failed'))
      .mockResolvedValue({
        id: 'recovering-model',
        display_name: 'recovering-model',
        supported_reasoning_levels: [{ effort: 'high', description: '' }],
        note: 'loaded',
      });
    const profile = reactive({ api_mode: 'gemini', model: 'recovering-model' });
    const scope = effectScope();
    const result = scope.run(() =>
      useApiReasoning(
        () => profile,
        () => true,
      ),
    )!;
    await nextTick();
    expect(result.note.value).toContain('无法读取思考档位');
    expect(result.details.value.supported_reasoning_levels).toEqual([]);
    profile.api_mode = 'responses';
    await nextTick();
    await nextTick();
    expect(result.note.value).toBe('loaded');
    profile.api_mode = 'gemini';
    await nextTick();
    await nextTick();
    expect(result.note.value).toBe('loaded');
    scope.stop();
  });
});
