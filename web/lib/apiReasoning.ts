import { computed, reactive, watch } from 'vue';
import { api } from './api';
import type { ModelDetails } from './modelCatalog';

interface ApiReasoningDetails extends ModelDetails {
  note: string;
}
const capabilities = reactive<Record<string, ApiReasoningDetails>>({});
const pending = new Map<string, Promise<ApiReasoningDetails>>();

/** Both editors and the composer use the transport's local protocol rules. */
export function useApiReasoning(
  profile: () => { api_mode?: string; model?: string },
  enabled: () => boolean,
) {
  const query = computed(() =>
    enabled() && profile().api_mode
      ? new URLSearchParams({ api_mode: profile().api_mode!, model: profile().model || '' }).toString()
      : '',
  );
  const errors = reactive<Record<string, string>>({});
  const details = computed<ModelDetails>(
    () =>
      capabilities[query.value] || {
        id: profile().model || '',
        display_name: profile().model || '',
        supported_reasoning_levels: [],
      },
  );
  const note = computed(() =>
    !query.value
      ? ''
      : errors[query.value] || capabilities[query.value]?.note || '正在读取当前接口的思考档位…',
  );
  watch(
    query,
    async (target) => {
      if (!target || capabilities[target]) return;
      delete errors[target];
      let request = pending.get(target);
      if (!request) {
        request = api('/settings/reasoning?' + target);
        pending.set(target, request!);
      }
      try {
        capabilities[target] = await request!;
      } catch (error) {
        errors[target] = `无法读取思考档位：${String(error)}`;
      } finally {
        if (pending.get(target) === request) pending.delete(target);
      }
    },
    { immediate: true },
  );
  return { details, note };
}
