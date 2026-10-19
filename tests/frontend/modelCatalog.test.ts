import { effectScope, nextTick, reactive } from 'vue';
import { describe, expect, it, vi } from 'vitest';
import { useModelCatalog } from '../../web/lib/modelCatalog';
import { modelEndpoint } from '../../web/lib/cachePolicy';
import type { ModelProfile, Provider } from '../../web/lib/settingsDocument';
import { api } from '../../web/lib/api';
import { chatgptState } from '../../web/lib/chatgptAccounts';
import { conversationModelProfile, selectProfileProvider } from '../../web/lib/modelConnections';
import { clone, difference, rebaseSettings, type SettingsDocument } from '../../web/lib/settingsDocument';

vi.mock('../../web/lib/api', () => ({ api: vi.fn() }));

describe('persistent model catalog UI', () => {
  it('restores per-provider connections before fetching and preserves rapid switches across save acknowledgements', async () => {
    vi.mocked(api).mockReset().mockResolvedValue({ models: [], updated_at: null });
    const original = {
      provider_id: 'first.provider',
      api_mode: 'gemini',
      api_path: 'gateway/v1beta',
      model: 'first-model',
      provider_connections: {},
      reply_tokens: 2048,
    } as ModelProfile;
    const draft = reactive(clone(original));
    const scope = effectScope();
    const catalog = scope.run(() =>
      useModelCatalog(
        () => draft,
        () => undefined,
      ),
    )!;
    Object.assign(draft, selectProfileProvider(draft, 'second.provider'));
    expect([draft.api_mode, draft.api_path, draft.model]).toEqual(['chat_completions', '', '']);
    Object.assign(draft, { api_mode: 'responses', api_path: 'proxy/v2', model: 'second-model' });
    await catalog.refresh();
    expect(api).toHaveBeenLastCalledWith('/settings/models', {
      provider_id: 'second.provider',
      api_mode: 'responses',
      api_path: 'proxy/v2',
    });
    const saved = {
      version: 6,
      full_agent: { model: original },
      half_agent: {},
      shared: {},
      revisions: { full_agent: 0, half_agent: 0, shared: 0 },
    } as unknown as SettingsDocument;
    const sent = difference(saved.full_agent, { model: draft });
    expect(sent['model.provider_connections']).toEqual({
      'first.provider': { api_mode: 'gemini', api_path: 'gateway/v1beta', model: 'first-model' },
    });
    const response = clone(saved);
    response.full_agent.model = clone(draft);
    Object.assign(draft, selectProfileProvider(draft, 'first.provider'));
    draft.model = 'latest-first-model';
    const current = clone(saved);
    current.full_agent.model = clone(draft);
    const acknowledged = rebaseSettings(saved, current, response, 'full_agent', sent);
    expect(acknowledged.full_agent.model).toEqual(draft);
    expect(draft.provider_connections).not.toHaveProperty('first.provider');
    expect(original.model).toBe('first-model');
    expect(draft.reply_tokens).toBe(2048);
    const reopened = clone(acknowledged.full_agent.model);
    const conversation = conversationModelProfile(reopened, {
      provider_id: 'second.provider',
      model: 'conversation-model',
      provider_models: { 'first.provider': 'conversation-first' },
    });
    expect([conversation.api_mode, conversation.api_path, conversation.model]).toEqual([
      'responses',
      'proxy/v2',
      'conversation-model',
    ]);
    expect(selectProfileProvider(conversation, 'first.provider').model).toBe('conversation-first');
    expect(reopened.model).toBe('latest-first-model');
    Object.assign(draft, selectProfileProvider(draft, 'official', true));
    expect([draft.api_mode, draft.api_path, draft.model]).toEqual(['responses', 'v1', '']);
    Object.assign(draft, selectProfileProvider(draft, 'first.provider'));
    await catalog.refresh();
    expect(api).toHaveBeenLastCalledWith('/settings/models', {
      provider_id: 'first.provider',
      api_mode: 'gemini',
      api_path: 'gateway/v1beta',
    });
    expect(draft.model).toBe('latest-first-model');
    scope.stop();
  });
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

  it('restores an official provider cache when the automatic refresh is offline', async () => {
    chatgptState.accounts = [
      {
        id: 'offline',
        label: 'Offline',
        email: '',
        status: 'connected',
        plan_enabled: true,
        generation: 'saved',
      },
    ];
    vi.mocked(api)
      .mockReset()
      .mockResolvedValueOnce({ models: ['saved-model'], updated_at: 100 })
      .mockRejectedValueOnce(new Error('offline'));
    const scope = effectScope();
    const result = scope.run(() =>
      useModelCatalog(
        () => ({ provider_id: 'offline-provider', api_mode: 'responses', api_path: '' }) as ModelProfile,
        () => ({ auth_mode: 'chatgpt_subscription', chatgpt_account_id: 'offline' }) as Provider,
      ),
    )!;
    await nextTick();
    await nextTick();
    await nextTick();
    expect(result.catalog.value?.models).toEqual(['saved-model']);
    await vi.waitFor(() => expect(result.error.message).toContain('offline'));
    scope.stop();
    chatgptState.accounts = [];
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

  it('uses the subscription catalogue without rewriting saved API parameters and refreshes on account change', async () => {
    chatgptState.accounts = [
      {
        id: 'account-a',
        label: 'A',
        email: 'fixture@example.test',
        status: 'connected',
        plan_enabled: true,
        generation: 'session-a',
      },
      {
        id: 'account-b',
        label: 'B',
        email: 'fixture@example.test',
        status: 'connected',
        plan_enabled: true,
        generation: 'session-b',
      },
    ];
    const calls: string[] = [];
    vi.mocked(api)
      .mockReset()
      .mockImplementation(async () => {
        calls.push('fetch');
        return { models: ['z', 'a'], model_details: [{ id: 'z', display_name: '首选' }], updated_at: 100 };
      });
    const profile = reactive({
      provider_id: 'subscription-catalogue',
      api_mode: 'gemini',
      api_path: 'v9',
      temperature: 0.9,
    } as ModelProfile);
    const provider = reactive({
      id: profile.provider_id,
      auth_mode: 'chatgpt_subscription',
      chatgpt_account_id: 'account-a',
    } as Provider);
    const scope = effectScope();
    const catalog = scope.run(() =>
      useModelCatalog(
        () => profile,
        () => provider,
        async () => {
          calls.push('save');
        },
      ),
    )!;
    await nextTick();
    await nextTick();
    expect(calls.slice(0, 2)).toEqual(['save', 'fetch']);
    await vi.waitFor(() =>
      expect(api).toHaveBeenCalledWith('/settings/models', {
        provider_id: profile.provider_id,
        api_mode: 'responses',
        api_path: 'v1',
      }),
    );
    expect(catalog.catalog.value?.models).toEqual(['z', 'a']);
    expect(profile.api_mode).toBe('gemini');
    expect(profile.api_path).toBe('v9');
    expect(profile.temperature).toBe(0.9);
    vi.mocked(api).mockResolvedValue({ models: ['other-account'], updated_at: 200 });
    provider.chatgpt_account_id = 'account-b';
    await nextTick();
    await nextTick();
    expect(catalog.catalog.value?.models).toEqual(['other-account']);
    scope.stop();
    chatgptState.accounts = [];
  });

  it('does not reuse a signed-out account catalogue or initiate subscription inference', async () => {
    chatgptState.accounts = [
      {
        id: 'logout-account',
        label: 'A',
        email: '',
        status: 'connected',
        plan_enabled: true,
        generation: 'before',
      },
    ];
    vi.mocked(api)
      .mockReset()
      .mockResolvedValue({ models: ['connected-model'], updated_at: 100 });
    const profile = { provider_id: 'logout-catalogue', api_mode: 'responses', api_path: '' } as ModelProfile;
    const provider = { auth_mode: 'chatgpt_subscription', chatgpt_account_id: 'logout-account' } as Provider;
    const scope = effectScope();
    const catalog = scope.run(() =>
      useModelCatalog(
        () => profile,
        () => provider,
      ),
    )!;
    await nextTick();
    await nextTick();
    expect(catalog.catalog.value?.models).toEqual(['connected-model']);
    vi.mocked(api).mockResolvedValue({ models: [], updated_at: null });
    chatgptState.accounts[0] = {
      ...chatgptState.accounts[0],
      status: 'signed_out',
      plan_enabled: false,
      generation: 'after',
    };
    await nextTick();
    await nextTick();
    expect(api).toHaveBeenLastCalledWith(
      '/settings/models?provider_id=logout-catalogue&api_mode=responses&api_path=v1',
    );
    expect(catalog.catalog.value?.models).toEqual([]);
    expect(vi.mocked(api).mock.calls.every(([path]) => path.startsWith('/settings/models'))).toBe(true);
    scope.stop();
    chatgptState.accounts = [];
  });
});
