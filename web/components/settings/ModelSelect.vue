<script setup lang="ts">
import { computed, ref } from 'vue';
import SelectMenu from '../common/SelectMenu.vue';
import Icon from '../common/Icon.vue';
import type { ModelDetails } from '../../lib/modelCatalog';
const props = defineProps<{
  modelValue: string;
  models: string[];
  details?: ModelDetails[];
  label: string;
  compact?: boolean;
  disabled?: boolean;
  error?: string;
  connection?: string;
  /** Requests the provider catalogue from inside the menu; omitted where a page has its own button. */
  refresh?: () => Promise<{ models: string[] }>;
}>();
const emit = defineEmits<{ 'update:modelValue': [value: string] }>();
const fetching = ref(false);
const fetchError = ref('');
const options = computed(() =>
  [...new Set([props.modelValue, ...props.models].filter(Boolean))].map((model) => {
    const name = props.details?.find((detail) => detail.id === model)?.display_name;
    return { value: model, label: model, description: name && name !== model ? name : undefined };
  }),
);
async function fetchModels() {
  // aria-disabled instead of disabled keeps keyboard focus inside the open menu.
  if (!props.refresh || fetching.value) return;
  fetching.value = true;
  fetchError.value = '';
  try {
    await props.refresh();
  } catch (e) {
    fetchError.value = String(e);
  } finally {
    fetching.value = false;
  }
}
</script>

<template>
  <SelectMenu
    :model-value="modelValue"
    :options="options"
    :label="label"
    :caption="compact ? '模型' : undefined"
    :compact="compact"
    :disabled="disabled"
    :error="fetchError || error"
    placeholder="选择模型"
    searchable
    allow-custom
    search-placeholder="搜索或输入模型 ID"
    :empty-text="refresh ? '暂无模型列表，可获取列表或输入模型 ID' : '暂无模型列表，可输入模型 ID 使用'"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <template v-if="connection" #context>
      <p class="model-connection muted">{{ connection }}</p>
    </template>
    <template v-if="refresh" #heading-action>
      <button
        type="button"
        class="model-fetch"
        :class="{ fetching }"
        :aria-disabled="fetching || undefined"
        title="向提供商请求模型列表并缓存"
        @click="fetchModels"
      >
        <Icon name="refresh" :size="13" />{{ fetching ? '正在获取…' : '获取模型列表' }}
      </button>
    </template>
  </SelectMenu>
</template>

<style scoped>
.model-connection {
  margin: 0;
  padding: 4px 10px 8px;
  font-size: var(--fs-sm);
  overflow-wrap: anywhere;
}
.model-fetch {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-height: 26px;
  margin: 0;
  padding: 3px 8px;
  border: 0;
  border-radius: var(--radius-xs);
  background: transparent;
  color: var(--text-muted);
  font: 500 var(--fs-xs) / 1.4 var(--font-ui);
  letter-spacing: 0;
  cursor: pointer;
}
.model-fetch:hover,
.model-fetch:focus-visible {
  background: var(--hover-wash);
  color: var(--accent-hover);
}
.model-fetch.fetching {
  cursor: progress;
}
.model-fetch.fetching :deep(svg) {
  animation: model-fetch-spin 0.9s linear infinite;
}
@keyframes model-fetch-spin {
  to {
    transform: rotate(360deg);
  }
}
@media (prefers-reduced-motion: reduce) {
  .model-fetch.fetching :deep(svg) {
    animation: none;
  }
}
</style>
