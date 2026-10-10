<script setup lang="ts">
import { computed } from 'vue';
const props = defineProps<{
  value: string;
  options: Array<{ value: string; label: string; disabled?: boolean }>;
}>();
const emit = defineEmits<{ change: [value: string] }>();
const selected = computed(
  () => props.options.find((option) => option.value === props.value)?.label || '跟随设置',
);
</script>

<template>
  <label class="effort-picker" :class="{ overridden: value !== 'inherit' }">
    <svg class="effort-icon" width="14" height="14" viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path
        d="m10 2 2.3 5.7L18 10l-5.7 2.3L10 18l-2.3-5.7L2 10l5.7-2.3L10 2Z"
        stroke="currentColor"
        stroke-width="1.3"
        stroke-linejoin="round"
      />
    </svg>
    <span class="effort-caption">思考</span>
    <span class="effort-value">{{ selected }}</span>
    <svg class="effort-chevron" width="10" height="10" viewBox="0 0 12 12" fill="none" aria-hidden="true">
      <path
        d="m3 4.5 3 3 3-3"
        stroke="currentColor"
        stroke-width="1.4"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
    </svg>
    <select
      :value="value"
      aria-label="本次思考强度"
      @change="emit('change', ($event.target as HTMLSelectElement).value)"
    >
      <option v-for="option in options" :key="option.value" :value="option.value" :disabled="option.disabled">
        {{ option.label }}
      </option>
    </select>
  </label>
</template>

<style scoped>
.effort-picker {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  padding: 6px 8px;
  min-height: 28px;
  border: 1px solid transparent;
  border-radius: 6px;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1;
  white-space: nowrap;
  transition:
    background 0.15s,
    color 0.15s;
}
.effort-picker:hover {
  background: var(--surface-hover);
  color: var(--text-secondary);
}
.effort-picker:focus-within {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.effort-icon {
  flex-shrink: 0;
  color: var(--text-muted);
}
.effort-value {
  color: var(--text-secondary);
}
.overridden .effort-icon,
.overridden .effort-value {
  color: var(--accent-hover);
}
.effort-caption {
  padding-right: 6px;
  border-right: 1px solid var(--border-control);
}
.effort-chevron {
  margin-left: 2px;
  color: var(--text-faint);
}
.effort-picker select {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  padding: 0;
  margin: 0;
  opacity: 0;
  cursor: pointer;
}
@media (prefers-reduced-motion: reduce) {
  .effort-picker {
    transition: none;
  }
}
</style>
