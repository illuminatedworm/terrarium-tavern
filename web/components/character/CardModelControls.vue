<script setup lang="ts">
import { computed, useId } from 'vue';
import type { ModelProfile, Provider } from '../../lib/settingsDocument';
import { useModelCatalog } from '../../lib/modelCatalog';
import { useApiReasoning } from '../../lib/apiReasoning';
import { effortOptions, modelEffortOptions } from '../../lib/reasoningEffort';
import { subscriptionProvider } from '../../lib/chatgptAccounts';
import EffortControl from '../settings/EffortControl.vue';
const props = defineProps<{
  profile: ModelProfile;
  provider?: Provider;
  model: string;
  effort: string;
  disabled: boolean;
}>();
const emit = defineEmits<{ 'update:model': [value: string]; 'update:effort': [value: string] }>();
const listId = useId();
const selected = computed(() => ({ ...props.profile, model: props.model || props.profile.model }));
const subscription = computed(() => subscriptionProvider(props.provider));
const { catalog, error } = useModelCatalog(
  () => selected.value,
  () => props.provider,
);
const { details } = useApiReasoning(
  () => selected.value,
  () => !subscription.value,
);
const models = computed(() => [
  ...new Set([props.profile.model, props.model, ...(catalog.value?.models || [])].filter(Boolean)),
]);
const efforts = computed(() =>
  modelEffortOptions(
    effortOptions,
    subscription.value
      ? catalog.value?.model_details?.find((item) => item.id === selected.value.model)
      : details.value,
    props.effort,
    !subscription.value,
  ),
);
function chooseModel(event: Event) {
  const value = (event.target as HTMLInputElement).value.trim();
  emit('update:model', value === props.profile.model ? '' : value);
}
</script>

<template>
  <div class="model-controls">
    <label
      class="model-picker"
      :title="
        error.message || `提供商：${provider?.name || '当前设置'}；可选择或输入模型名称，清空后跟随设置`
      "
    >
      <span>模型</span
      ><input
        :value="selected.model"
        :list="listId"
        :disabled="disabled"
        aria-label="本次模型"
        placeholder="跟随设置"
        @change="chooseModel"
        @keydown.enter.prevent="chooseModel"
      />
      <datalist :id="listId"><option v-for="name in models" :key="name" :value="name" /></datalist>
    </label>
    <EffortControl
      :value="effort"
      :options="efforts"
      :disabled="disabled"
      @change="emit('update:effort', $event)"
    />
  </div>
</template>

<style scoped>
.model-controls {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 2px;
  min-width: 0;
}
.model-picker {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  padding: 6px 8px;
  min-height: 28px;
  border: 1px solid transparent;
  border-radius: 6px;
  color: var(--text-muted);
  font: 11px/1 var(--font-ui);
}
.model-picker:hover {
  background: var(--surface-hover);
}
.model-picker > span {
  padding-right: 6px;
  border-right: 1px solid var(--border-control);
}
.model-picker input {
  width: 170px;
  min-width: 0;
  margin: 0;
  padding: 0;
  border: 0;
  border-radius: 0;
  background: transparent;
  color: var(--text-secondary);
  font: inherit;
}
@media (max-width: 620px) {
  .model-picker input {
    width: 100px;
  }
}
</style>
