import { onBeforeUnmount, watch, type Ref } from 'vue';

/** Dismiss transient notifications without changing persisted failure records. */
export function useTimedError(error: Ref<string>, delay = 8000) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  watch(
    error,
    (value) => {
      clearTimeout(timer);
      if (value)
        timer = setTimeout(() => {
          error.value = '';
        }, delay);
    },
    { immediate: true },
  );
  onBeforeUnmount(() => clearTimeout(timer));
}
