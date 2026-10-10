import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { api } from '../lib/api';

export function useContextOperations(session: () => any, updated: (value: any) => void) {
  const operation = ref<any>(null),
    error = ref(''),
    submitting = ref(false);
  const busy = computed(() => submitting.value || ['queued', 'running'].includes(operation.value?.status));
  let timer: ReturnType<typeof setTimeout> | undefined;
  let generation = 0;
  async function refresh() {
    const sid = session()?.id,
      ticket = generation;
    if (!sid || session()?.channel_origin !== 'chatgpt' || session()?.agent_mode !== 'full_agent') return;
    try {
      const result = await api(`/sessions/${sid}/context-operation`);
      if (generation !== ticket || session()?.id !== sid) return;
      const previous = operation.value;
      operation.value = result;
      if (result?.status === 'completed' && previous?.status !== 'completed') {
        const value = await api(`/sessions/${sid}`);
        if (generation === ticket && session()?.id === sid) updated(value);
      }
      if (['queued', 'running'].includes(result?.status)) timer = setTimeout(refresh, 1000);
    } catch (e: any) {
      if (generation === ticket) {
        error.value = e.message;
        if (busy.value) timer = setTimeout(refresh, 3000);
      }
    }
  }
  watch(
    [() => session()?.id, () => session()?.channel_origin],
    () => {
      generation++;
      clearTimeout(timer);
      operation.value = null;
      submitting.value = false;
      error.value = '';
      void refresh();
    },
    { immediate: true },
  );
  async function start(kind: 'enable_compact' | 'compact' | 'enable_local') {
    const sid = session()?.id,
      ticket = generation;
    if (!sid || busy.value) return;
    submitting.value = true;
    error.value = '';
    try {
      const result = await api(`/sessions/${sid}/context-operations`, {
        operation_id: crypto.randomUUID(),
        expected_revision: session().revision,
        kind,
      });
      if (ticket !== generation) return;
      operation.value = result;
      clearTimeout(timer);
      void refresh();
    } catch (e: any) {
      if (ticket === generation) error.value = e.message;
    } finally {
      if (ticket === generation) submitting.value = false;
    }
  }
  async function control(action: 'cancel' | 'resume') {
    const sid = session()?.id,
      op = operation.value,
      ticket = generation;
    if (!sid || !op) return;
    error.value = '';
    try {
      const value = await api(`/sessions/${sid}/context-operations/${op.id}/${action}`, {});
      if (ticket !== generation) return;
      operation.value = value;
      clearTimeout(timer);
      void refresh();
    } catch (e: any) {
      if (ticket === generation) error.value = e.message;
    }
  }
  async function automatic(enabled: boolean) {
    const sid = session()?.id,
      ticket = generation;
    try {
      const value = await api(
        `/sessions/${sid}/compact-auto`,
        { enabled, expected_revision: session().revision },
        'PUT',
      );
      if (ticket === generation) updated(value);
    } catch (e: any) {
      if (ticket === generation) error.value = e.message;
    }
  }
  onBeforeUnmount(() => {
    generation++;
    clearTimeout(timer);
  });
  return { operation, error, busy, start, control, automatic };
}
export type ContextOperations = ReturnType<typeof useContextOperations>;
