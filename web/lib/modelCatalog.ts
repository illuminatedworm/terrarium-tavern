import { computed, reactive, watch } from 'vue';
import { api } from './api';
import type { ModelProfile, Provider } from './settingsDocument';

interface Catalog {
  models: string[];
  updated_at: number | null;
}
const catalogs = reactive<Record<string, Catalog>>({});
const generations = new Map<string, number>();
const refreshes = new Map<string, Promise<Catalog>>();

export function useModelCatalog(profile: () => ModelProfile, provider: () => Provider | undefined) {
  const request = computed(() => ({
    provider_id: profile().provider_id,
    api_mode: profile().api_mode,
    api_path:
      profile()
        .api_path.trim()
        .replace(/^\/+|\/+$/g, '') || (profile().api_mode === 'gemini' ? 'v1beta' : 'v1'),
  }));
  const key = computed(() =>
    JSON.stringify([request.value, provider()?.base_url, provider()?.has_api_key, provider()?.api_key]),
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
    key,
    () => {
      const target = key.value;
      void load().catch((e) => {
        if (key.value === target) error.message = String(e);
      });
    },
    { immediate: true },
  );
  return { catalog, error, refresh: () => load(true) };
}
