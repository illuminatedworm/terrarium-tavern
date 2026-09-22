import { computed, ref, watch } from 'vue';

export const HISTORY_BATCH = 60;

/** Only limit rendering. Full history stays available to scripts and story operations. */
export function useStoryHistory<T extends { id: string }>(
  getSession: () => { id: string; messages: T[] } | null,
) {
  const start = ref(0);
  const messages = computed(() => getSession()?.messages || []);
  const hidden = computed(() => Math.min(start.value, Math.max(0, messages.value.length - 1)));
  const visible = computed(() => messages.value.slice(hidden.value));
  function collapse() {
    start.value = Math.max(0, messages.value.length - HISTORY_BATCH);
  }
  watch(() => getSession()?.id, collapse, { immediate: true, flush: 'sync' });
  return {
    visible,
    hidden,
    collapse,
    // Keep the start fixed as new replies arrive; do not unmount what is being read.
    showEarlier() {
      start.value = Math.max(0, hidden.value - HISTORY_BATCH);
    },
    reveal(id: string) {
      const index = messages.value.findIndex((message) => message.id === id);
      if (index >= 0) start.value = Math.min(hidden.value, index);
    },
  };
}
