<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { api } from '../../lib/api';
import {
  adapterRule,
  updateAdapterRule,
  type MessageAdapterConfig,
  type MessageTypeDefinition,
} from '../../lib/messageAdapter';
const props = defineProps<{
  modelValue?: MessageAdapterConfig;
  apiMode: string;
  lane: string;
  types?: MessageTypeDefinition[];
}>();
const emit = defineEmits<{ 'update:modelValue': [value: MessageAdapterConfig] }>();
const loaded = ref<MessageTypeDefinition[]>([]),
  error = ref('');
const search = ref(''),
  showFixed = ref(false);
let generation = 0;
watch(
  () => [props.lane, props.types],
  async () => {
    const token = ++generation;
    if (props.types) return;
    try {
      const result = await api(`/message-adapter/types?lane=${encodeURIComponent(props.lane)}`);
      if (token === generation) {
        loaded.value = result;
        error.value = '';
      }
    } catch (e) {
      if (token === generation) error.value = String(e);
    }
  },
  { immediate: true },
);
const types = computed(() =>
  (props.types || loaded.value).filter(
    (type) =>
      (showFixed.value || type.configurable) && type.label.toLowerCase().includes(search.value.toLowerCase()),
  ),
);
const rule = (type: MessageTypeDefinition) => adapterRule(props.modelValue, props.apiMode, type);
function update(type: MessageTypeDefinition, patch: { role?: string; wrap?: boolean }) {
  if (type.configurable)
    emit('update:modelValue', updateAdapterRule(props.modelValue, props.apiMode, type, patch));
}
function reset(type: MessageTypeDefinition) {
  const next: MessageAdapterConfig = JSON.parse(
    JSON.stringify(props.modelValue || { version: 1, mappings: {} }),
  );
  if (next.mappings[props.apiMode]) delete next.mappings[props.apiMode][type.id];
  emit('update:modelValue', next);
}
</script>
<template>
  <section class="message-adapter-editor" aria-label="消息适配映射">
    <div class="adapter-filter">
      <label>筛选消息类型<input v-model="search" type="search" /></label
      ><label class="adapter-check"><input v-model="showFixed" type="checkbox" />显示固定类型</label>
    </div>
    <p v-if="error" class="danger" role="alert">{{ error }}</p>
    <div v-for="type in types" :key="type.id" class="adapter-row" :data-message-type="type.id">
      <div>
        <strong>{{ type.label }}</strong
        ><small v-if="type.appendable">可以追加更新</small
        ><small v-if="type.tag">&lt;{{ type.tag }}&gt;</small>
      </div>
      <label v-if="type.configurable"
        >发送角色<select
          :aria-label="type.label + '发送角色'"
          :value="rule(type).role"
          @change="update(type, { role: ($event.target as HTMLSelectElement).value })"
        >
          <option v-for="role in ['system', 'developer', 'user', 'assistant']" :key="role" :value="role">
            {{ role }}
          </option>
        </select></label
      >
      <span v-else class="adapter-fixed">{{ rule(type).role }} · 固定</span>
      <label class="adapter-check"
        ><input
          :aria-label="type.label + '标签包裹'"
          type="checkbox"
          :checked="rule(type).wrap"
          :disabled="!type.configurable || !type.tag"
          @change="update(type, { wrap: ($event.target as HTMLInputElement).checked })"
        />标签包裹</label
      >
      <button v-if="type.configurable" type="button" @click="reset(type)">默认</button>
    </div>
    <p class="adapter-note">
      同一类型的初始版本与追加版本使用相同映射。选择 system 或 developer
      后，部分接口或模型可能拒绝中途消息；设置会保留，不会自动更改。
    </p>
  </section>
</template>
<style scoped>
.message-adapter-editor {
  min-width: 0;
}
.adapter-filter {
  display: flex;
  align-items: end;
  flex-wrap: wrap;
  gap: 16px;
  margin: 12px 0;
}
.adapter-filter > label:first-child {
  flex: 1;
  min-width: 160px;
}
.adapter-row {
  display: grid;
  grid-template-columns: minmax(140px, 1fr) minmax(120px, 160px) auto auto;
  gap: 12px;
  align-items: center;
  padding: 12px 0;
  border-bottom: 1px solid var(--border-subtle);
}
.adapter-row strong,
.adapter-row small {
  display: block;
  overflow-wrap: anywhere;
}
.adapter-row small,
.adapter-note,
.adapter-fixed {
  color: var(--text-secondary);
  font-size: 12px;
}
.adapter-check {
  display: flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}
.adapter-check input {
  width: auto;
}
.adapter-note {
  line-height: 1.7;
}
@media (max-width: 650px) {
  .adapter-row {
    grid-template-columns: minmax(0, 1fr) minmax(100px, 140px);
  }
}
</style>
