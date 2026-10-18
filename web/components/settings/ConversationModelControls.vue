<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import type { ConversationModelSelection, ModelProfile, Provider } from '../../lib/settingsDocument';
import { channelProviders, channelLabels, type Channel } from '../../lib/channels';
import { useModelCatalog } from '../../lib/modelCatalog';
import { useApiReasoning } from '../../lib/apiReasoning';
import { modelConnectionSummary, selectProfileProvider } from '../../lib/modelConnections';
import { effortOptions, modelEffortOptions } from '../../lib/reasoningEffort';
import EffortControl from './EffortControl.vue';
import SelectMenu from '../common/SelectMenu.vue';
import ModelSelect from './ModelSelect.vue';

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
const providerOptions = computed(() =>
  providers.value.map((provider) => ({ value: provider.id, label: provider.name })),
);
const provider = computed(() => providers.value.find((item) => item.id === draft.value.provider_id));
const subscription = computed(() => props.channel === 'chatgpt');
const {
  catalog,
  error: catalogError,
  refresh,
} = useModelCatalog(
  () => draft.value,
  () => provider.value,
  undefined,
  () => !!provider.value,
);
const { details } = useApiReasoning(
  () => props.profile,
  () => !subscription.value,
);
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
  if (props.disabled || !provider.value) return;
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
function chooseProvider(value: string) {
  draft.value = selectProfileProvider(draft.value, value, subscription.value);
  save();
}
function chooseModel(value: string) {
  draft.value.model = value;
  save();
}
</script>

<template>
  <div class="model-controls" :title="`${channelLabels[channel]} · 仅可更换同渠道供应商`">
    <SelectMenu
      class="provider-picker"
      :model-value="draft.provider_id"
      :options="providerOptions"
      :disabled="disabled"
      label="对话供应商"
      placeholder="选择供应商"
      compact
      @update:model-value="chooseProvider"
    />
    <ModelSelect
      class="model-picker"
      :model-value="draft.model"
      :models="catalog?.models || []"
      :details="catalog?.model_details"
      :error="catalogError.message"
      :refresh="refresh"
      :connection="modelConnectionSummary(draft, provider)"
      :disabled="disabled || !provider"
      label="对话模型"
      compact
      @update:model-value="chooseModel"
    />
    <EffortControl
      :value="effort"
      :options="efforts"
      :disabled="disabled"
      @change="emit('update:effort', $event)"
    />
  </div>
</template>

<style scoped>
/* Provider, model and effort read as one segmented chip in the composer. */
.model-controls {
  display: inline-flex;
  align-items: center;
  flex-shrink: 1;
  gap: 1px;
  min-width: 0;
  max-width: 100%;
  padding: 2px;
  border-radius: 11px;
  background: rgb(255 255 255 / 0.035);
  box-shadow: inset 0 0 0 1px var(--border-subtle);
}
.provider-picker {
  flex: none;
  width: 128px;
}
.model-picker {
  flex: none;
  width: 180px;
}
.effort-picker {
  flex: none;
  width: 140px;
}
@container composer (max-width: 760px) {
  .model-controls {
    display: flex;
  }
  .provider-picker,
  .model-picker,
  .effort-picker {
    flex: 1 1 0;
    width: auto;
    min-width: 0;
  }
}
</style>
