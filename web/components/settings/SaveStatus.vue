<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue';
const props = defineProps<{ state: string; revision: string }>();
const visible = ref(true);
let timer: ReturnType<typeof setTimeout> | undefined;
watch(
  () => [props.state, props.revision],
  () => {
    clearTimeout(timer);
    visible.value = true;
    if (props.state === '已保存')
      timer = setTimeout(() => {
        visible.value = false;
      }, 2000);
  },
  { immediate: true },
);
onBeforeUnmount(() => clearTimeout(timer));
</script>
<template>
  <span
    class="settings-save-status"
    :class="{ failed: state === '保存失败', 'save-status-hidden': !visible }"
    :aria-hidden="!visible"
    :data-state="state"
    role="status"
  >
    <span class="save-dot" aria-hidden="true"></span>{{ state }}
  </span>
</template>
<style scoped>
.settings-save-status {
  opacity: 1;
  visibility: visible;
  transition:
    opacity 0.5s ease,
    visibility 0.5s;
}
.settings-save-status.save-status-hidden {
  opacity: 0;
  visibility: hidden;
  pointer-events: none;
}
@media (prefers-reduced-motion: reduce) {
  .settings-save-status {
    transition: none;
  }
}
</style>
