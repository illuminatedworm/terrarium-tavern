import { nextTick, onBeforeUnmount, ref, shallowRef, watch, type Ref } from 'vue';
import { loadVisuals, observeVisuals, type VisualOutput } from '../lib/visuals';

/** Optional data loading only: plain replies create no observer or graphic component. */
export function useStoryVisuals(
  identity: () => { enabled: boolean; session: string; message: string },
  anchor: Ref<HTMLElement | undefined>,
) {
  const output = shallowRef<VisualOutput | null>(null);
  const error = ref('');
  const loading = ref(false);
  let generation = 0;
  let stopObserving: (() => void) | undefined;
  async function load() {
    const current = ++generation;
    const source = identity();
    loading.value = true;
    error.value = '';
    try {
      const result = await loadVisuals(source.session, source.message);
      if (current === generation) output.value = result;
    } catch (cause) {
      if (current === generation) error.value = cause instanceof Error ? cause.message : String(cause);
    } finally {
      if (current === generation) loading.value = false;
    }
  }
  watch(
    () => {
      const source = identity();
      return `${source.enabled}:${source.session}:${source.message}`;
    },
    async () => {
      const current = ++generation;
      stopObserving?.();
      output.value = null;
      error.value = '';
      if (!identity().enabled) return;
      await nextTick();
      if (current !== generation || !anchor.value) return;
      // Observe the message body, not an end marker: a visual may precede all
      // prose, and jumping into a long reply must still make its graphics load.
      stopObserving = observeVisuals(anchor.value.parentElement!, () => void load());
    },
    { immediate: true, flush: 'post' },
  );
  onBeforeUnmount(() => {
    generation++;
    stopObserving?.();
  });
  return { output, error, loading, retry: load };
}
