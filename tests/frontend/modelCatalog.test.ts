import { effectScope, nextTick, reactive } from 'vue';
import { describe, expect, it, vi } from 'vitest';
import { useModelCatalog } from '../../web/lib/modelCatalog';
import { modelEndpoint } from '../../web/lib/cachePolicy';
import type { ModelProfile, Provider } from '../../web/lib/settingsDocument';
import { api } from '../../web/lib/api';

vi.mock('../../web/lib/api', () => ({ api: vi.fn() }));

describe('persistent model catalog UI', () => {
  it('reads cached lists on entry and only refreshes explicitly, preserving a failed refresh', async () => {
    const cached = { models: ['cached-model'], updated_at: 100 };
    vi.mocked(api).mockReset().mockResolvedValue(cached);
    const profile = reactive({ provider_id: 'cache-read', api_mode: 'gemini', api_path: '' } as ModelProfile);
    const scope = effectScope();
    const catalog = scope.run(() =>
      useModelCatalog(
        () => profile,
        () => undefined,
      ),
    )!;
    await nextTick();
    expect(api).toHaveBeenCalledWith(
      '/settings/models?provider_id=cache-read&api_mode=gemini&api_path=v1beta',
    );
    expect(catalog.catalog.value).toEqual(cached);
    vi.mocked(api).mockRejectedValueOnce(new Error('offline'));
    await expect(catalog.refresh()).rejects.toThrow('offline');
    expect(catalog.catalog.value).toEqual(cached);
    expect(api).toHaveBeenLastCalledWith('/settings/models', {
      provider_id: 'cache-read',
      api_mode: 'gemini',
      api_path: 'v1beta',
    });
    scope.stop();
  });

  it('shares refreshed lists between profiles using the same connection', async () => {
    vi.mocked(api).mockReset().mockResolvedValue({ models: [], updated_at: null });
    const profile = { provider_id: 'shared-list', api_mode: 'responses', api_path: '' } as ModelProfile;
    const scope = effectScope();
    const [first, second] = scope.run(() => [
      useModelCatalog(
        () => profile,
        () => undefined,
      ),
      useModelCatalog(
        () => profile,
        () => undefined,
      ),
    ])!;
    await nextTick();
    vi.mocked(api).mockResolvedValueOnce({ models: ['new-model'], updated_at: 200 });
    await first.refresh();
    expect(second.catalog.value?.models).toEqual(['new-model']);
    scope.stop();
  });

  it('does not show an older connection response after switching providers', async () => {
    let finishOld!: (value: unknown) => void;
    vi.mocked(api)
      .mockReset()
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finishOld = resolve;
          }),
      )
      .mockResolvedValue({ models: ['other-model'], updated_at: 200 });
    const profile = reactive({
      provider_id: 'old-provider',
      api_mode: 'responses',
      api_path: '',
    } as ModelProfile);
    const provider = { base_url: 'https://fixture.test' } as Provider;
    const scope = effectScope();
    const result = scope.run(() =>
      useModelCatalog(
        () => profile,
        () => provider,
      ),
    )!;
    profile.provider_id = 'other-provider';
    await nextTick();
    await nextTick();
    finishOld({ models: ['old-model'], updated_at: 100 });
    await nextTick();
    expect(result.catalog.value?.models).toEqual(['other-model']);
    scope.stop();
  });

  it('lets an editor opened during a refresh receive the new list', async () => {
    vi.mocked(api).mockReset().mockResolvedValue({ models: [], updated_at: null });
    const profile = { provider_id: 'pending-refresh', api_mode: 'responses', api_path: '' } as ModelProfile;
    const scope = effectScope();
    const first = scope.run(() =>
      useModelCatalog(
        () => profile,
        () => undefined,
      ),
    )!;
    await nextTick();
    let finish!: (value: unknown) => void;
    vi.mocked(api).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const refresh = first.refresh();
    const second = scope.run(() =>
      useModelCatalog(
        () => profile,
        () => undefined,
      ),
    )!;
    finish({ models: ['refreshed-model'], updated_at: 300 });
    await refresh;
    expect(api).toHaveBeenCalledTimes(2);
    expect(second.catalog.value?.models).toEqual(['refreshed-model']);
    scope.stop();
  });

  it('previews explicit native paths without rewriting them', () => {
    expect(modelEndpoint('https://fixture.test', 'gemini', 'model', 'v1')).toBe(
      'https://fixture.test/v1/models/model:generateContent',
    );
    expect(modelEndpoint('https://fixture.test', 'anthropic', 'model', '/v2/')).toBe(
      'https://fixture.test/v2/messages',
    );
    expect(modelEndpoint('https://fixture.test', 'gemini', 'model', '')).toBe(
      'https://fixture.test/v1beta/models/model:generateContent',
    );
  });
});
