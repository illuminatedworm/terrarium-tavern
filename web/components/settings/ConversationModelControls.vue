<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue';
import type { ConversationModelSelection, ModelProfile, Provider } from '../../lib/settingsDocument';
import { channelProviders, channelLabels, type Channel } from '../../lib/channels';
import { useModelCatalog } from '../../lib/modelCatalog';
import { useApiReasoning } from '../../lib/apiReasoning';
import { effortOptions, modelEffortOptions } from '../../lib/reasoningEffort';
import EffortControl from './EffortControl.vue';

const props = defineProps<{
  profile: ModelProfile;
  providers: Provider[];
  channel: Channel;
  effort: string;
  disabled: boolean;
}>();
const emit = defineEmits<{
  select: [selection: ConversationModelSelection];
  'update:effort': [value: string];
}>();
const listId = useId();
const draft = ref({ ...props.profile });
watch(
  () => props.profile,
  (value) => {
    draft.value = { ...value };
  },
  { deep: true },
);
watch(
  () => props.disabled,
  (value) => {
    if (!value) draft.value = { ...props.profile };
  },
);
const providers = computed(() => channelProviders(props.providers, props.channel));
const provider = computed(() => providers.value.find((item) => item.id === draft.value.provider_id));
const subscription = computed(() => props.channel === 'chatgpt');
const { catalog, error } = useModelCatalog(
  () => draft.value,
  () => provider.value,
  undefined,
  () => !!provider.value,
);
const { details } = useApiReasoning(
  () => props.profile,
  () => !subscription.value,
);
const models = computed(() => [
  ...new Set(
    [
      props.profile.provider_id === draft.value.provider_id ? props.profile.model : '',
      ...(catalog.value?.models || []),
    ].filter(Boolean),
  ),
]);
const efforts = computed(() =>
  modelEffortOptions(
    effortOptions,
    subscription.value
      ? catalog.value?.model_details?.find((item) => item.id === props.profile.model)
      : details.value,
    props.effort,
    !subscription.value,
  ),
);
function save() {
  if (props.disabled || !draft.value.model.trim() || !provider.value) return;
  if (
    ['provider_id', 'model'].every(
      (key) => draft.value[key as keyof ModelProfile] === props.profile[key as keyof ModelProfile],
    )
  )
    return;
  emit('select', {
    provider_id: draft.value.provider_id,
    model: draft.value.model.trim(),
  });
}
function chooseProvider(event: Event) {
  draft.value.provider_id = (event.target as HTMLSelectElement).value;
  // Keep the typed model when switching equivalent providers; it may also be
  // changed manually when a provider does not publish a catalogue.
  save();
}
</script>

<template>
  <div class="model-controls" :title="`${channelLabels[channel]} · 仅可更换同渠道供应商`">
    <label class="provider-picker"
      ><span class="sr-only">供应商</span>
      <select
        :value="draft.provider_id"
        :disabled="disabled"
        aria-label="对话供应商"
        :title="provider?.name"
        @change="chooseProvider"
      >
        <option v-if="!provider" :value="draft.provider_id" disabled>选择供应商</option>
        <option v-for="item in providers" :key="item.id" :value="item.id">{{ item.name }}</option>
      </select>
    </label>
    <label class="model-picker" :title="error.message || '选择或输入模型名称'">
      <span>模型</span
      ><input
        v-model="draft.model"
        :list="listId"
        :disabled="disabled || !provider"
        aria-label="对话模型"
        :title="draft.model"
        placeholder="输入模型名称"
        @change="save"
        @blur="save"
        @keydown.enter.prevent="save"
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
  max-width: 100%;
}
.provider-picker,
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
.provider-picker:hover,
.model-picker:hover {
  background: var(--surface-hover);
}
.model-picker > span {
  padding-right: 6px;
  border-right: 1px solid var(--border-control);
}
.model-picker input,
.provider-picker select {
  min-width: 0;
  margin: 0;
  padding: 0;
  border: 0;
  border-radius: 0;
  background: transparent;
  color: var(--text-secondary);
  font: inherit;
}
.model-picker input {
  width: 150px;
}
.provider-picker select {
  width: 100px;
  padding-right: 14px !important;
  background-position: right center !important;
  text-overflow: ellipsis;
}
.provider-picker option {
  background: var(--surface);
}
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
}
@media (max-width: 620px) {
  .model-controls :deep(.effort-caption) {
    display: none;
  }
  .provider-picker,
  .model-picker {
    padding: 6px 4px;
    gap: 4px;
  }
  .model-picker > span {
    padding-right: 4px;
  }
  .model-picker input {
    width: 70px;
  }
  .provider-picker select {
    width: 70px;
  }
}
</style>
