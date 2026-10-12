<script setup lang="ts">
import { computed } from 'vue';
import SelectMenu from '../common/SelectMenu.vue';
import type { ModelDetails } from '../../lib/modelCatalog';
const props = defineProps<{
  modelValue: string;
  models: string[];
  details?: ModelDetails[];
  label: string;
  compact?: boolean;
  disabled?: boolean;
  error?: string;
}>();
const emit = defineEmits<{ 'update:modelValue': [value: string] }>();
const options = computed(() =>
  [...new Set([props.modelValue, ...props.models].filter(Boolean))].map((model) => {
    const name = props.details?.find((detail) => detail.id === model)?.display_name;
    return { value: model, label: model, description: name && name !== model ? name : undefined };
  }),
);
</script>

<template>
  <SelectMenu
    :model-value="modelValue"
    :options="options"
    :label="label"
    :caption="compact ? '模型' : undefined"
    :compact="compact"
    :disabled="disabled"
    :error="error"
    placeholder="选择模型"
    searchable
    allow-custom
    search-placeholder="搜索或输入模型 ID"
    empty-text="暂无模型列表，可输入模型 ID 使用"
    @update:model-value="emit('update:modelValue', $event)"
  />
</template>
