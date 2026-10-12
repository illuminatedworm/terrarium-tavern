<script setup lang="ts">
import { computed } from 'vue';
import SelectMenu from '../common/SelectMenu.vue';
const props = defineProps<{
  value: string;
  disabled?: boolean;
  options: Array<{ value: string; label: string; disabled?: boolean }>;
}>();
const emit = defineEmits<{ change: [value: string] }>();
const options = computed(() =>
  props.options.map((option) => {
    const [label, ...description] = option.label.split(' · ');
    return { ...option, label, description: description.join(' · ') || undefined };
  }),
);
</script>

<template>
  <SelectMenu
    class="effort-picker"
    :model-value="value"
    :options="options"
    :disabled="disabled"
    :highlighted="value !== 'inherit'"
    label="本次思考强度"
    caption="思考"
    compact
    @update:model-value="emit('change', $event)"
  >
    <template #icon>
      <svg width="14" height="14" viewBox="0 0 20 20" fill="none" aria-hidden="true">
        <path
          d="m10 2 2.3 5.7L18 10l-5.7 2.3L10 18l-2.3-5.7L2 10l5.7-2.3L10 2Z"
          stroke="currentColor"
          stroke-width="1.3"
          stroke-linejoin="round"
        />
      </svg>
    </template>
  </SelectMenu>
</template>
