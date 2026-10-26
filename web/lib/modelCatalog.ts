import { computed, reactive, watch } from 'vue';
import { api } from './api';
import type { ModelProfile, Provider } from './settingsDocument';
import { chatgptGeneration, chatgptState, subscriptionProvider } from './chatgptAccounts';

export interface ModelDetails {
  id: string;
  display_name: string;
  supported_reasoning_levels?: Array<{ effort: string; description: string }>;
  unavailable_reasoning_levels?: Array<{ effort: string; reason: string }>;
  default_reasoning_level?: string;
}
interface Catalog {
  models: string[];
  updated_at: number | null;
  model_details?: ModelDetails[];
}
const catalogs = reactive<Record<string, Catalog>>({});
const generations = new Map<string, number>();
const refreshes = new Map<string, Promise<Catalog>>();

export function useModelCatalog(
  profile: () => ModelProfile,
  provider: () => Provider | undefined,
  beforeRefresh?: () => Promise<unknown>,
  enabled: () => boolean = () => true,
) {
  const request = computed(() => ({
    provider_id: profile().provider_id,
    api_mode: subscriptionProvider(provider()) ? 'responses' : profile().api_mode,
    api_path: subscriptionProvider(provider())
      ? 'v1'
      : profile()
          .api_path.trim()
          .replace(/^\/+|\/+$/g, '') || (profile().api_mode === 'gemini' ? 'v1beta' : 'v1'),
  }));
  const key = computed(() =>
    JSON.stringify([
      // Roles share only the provider's catalogue. Refresh requests keep each
      // role's own protocol/path, and selecting a model never changes another role.
      request.value.provider_id,
      provider()?.base_url,
      provider()?.has_api_key,
      provider()?.api_key || '',
      provider()?.auth_mode,
      provider()?.chatgpt_account_id,
      chatgptGeneration(provider()),
    ]),
  );
  const catalog = computed(() => catalogs[key.value]);
  const error = reactive({ message: '' });
  async function load(refresh = false) {
    const target = key.value;
    const pending = refreshes.get(target);
    if (pending) return pending;
    const generation = (generations.get(target) || 0) + 1;
    generations.set(target, generation);
    error.message = '';
    const fetchCatalog = async (): Promise<Catalog> => {
      const result = refresh
        ? await api('/settings/models', request.value)
        : await api('/settings/models?' + new URLSearchParams(request.value));
      if (generations.get(target) === generation) catalogs[target] = result;
      return result;
    };
    if (!refresh) return fetchCatalog();
    // Other editors joining during a refresh share it instead of racing a stale read.
    const work = fetchCatalog();
    refreshes.set(target, work);
    try {
      return await work;
    } finally {
      refreshes.delete(target);
    }
  }
  watch(
    () => [enabled() ? key.value : '', enabled()],
    async () => {
      if (!enabled()) return;
      const target = key.value;
      try {
        const connected =
          subscriptionProvider(provider()) &&
          chatgptState.accounts.some(
            (account) => account.id === provider()?.chatgpt_account_id && account.plan_enabled,
          );
        if (connected) await beforeRefresh?.();
        if (key.value !== target) return;
        // Restore the durable catalogue before any optional network refresh.
        // An offline subscription must keep its last successfully saved list too.
        await load(false);
        if (connected && key.value === target) await load(true);
      } catch (e) {
        if (key.value === target) error.message = String(e);
      }
    },
    { immediate: true },
  );
  return { catalog, error, refresh: () => load(true) };
}
