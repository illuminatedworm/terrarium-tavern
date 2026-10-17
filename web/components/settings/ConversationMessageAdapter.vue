<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { api } from '../../lib/api';
import { adapterApis, type AdapterSettings } from '../../lib/messageAdapter';
import MessageAdapterEditor from './MessageAdapterEditor.vue';
const props = defineProps<{ endpoint: string }>();
const state = ref<AdapterSettings>(),
  laneId = ref(''),
  protocol = ref('chat_completions');
const error = ref(''),
  notice = ref(''),
  saving = ref(false);
const lane = computed(() => state.value?.lanes.find((item) => item.id === laneId.value));
const adapterEditor = ref<InstanceType<typeof MessageAdapterEditor>>();
let generation = 0;
async function load() {
  const token = ++generation;
  try {
    const result: AdapterSettings = await api(props.endpoint);
    if (token !== generation) return;
    state.value = result;
    laneId.value = result.lanes[0].id;
    protocol.value = result.lanes[0].api_mode;
    error.value = '';
  } catch (e) {
    if (token === generation) error.value = String(e);
  }
}
watch(() => props.endpoint, load, { immediate: true });
watch(lane, (value) => {
  if (value) protocol.value = value.api_mode;
});
async function save() {
  if (!state.value || saving.value) return;
  saving.value = true;
  error.value = '';
  notice.value = '';
  try {
    const result: AdapterSettings = await api(
      props.endpoint,
      { expected_adapter_revision: state.value.revision, config: state.value.config },
      'PUT',
    );
    state.value = result;
    notice.value = '已保存，下一次模型调用立即生效。';
  } catch (e) {
    error.value = String(e);
  } finally {
    saving.value = false;
  }
}
</script>
<template>
  <section aria-label="当前对话消息适配">
    <h2>消息适配</h2>
    <p>仅影响当前对话。保存后，下一次模型调用会重新转换整份上下文；运行中的回合也可以修改。</p>
    <template v-if="state && lane">
      <div class="adapter-actions">
        <label
          >调用模型<select v-model="laneId">
            <option v-for="item in state.lanes" :key="item.id" :value="item.id">{{ item.label }}</option>
          </select></label
        ><label
          >接口协议<select
            v-model="protocol"
            @change="adapterEditor?.resetRoles(($event.target as HTMLSelectElement).value)"
          >
            <option v-for="item in adapterApis" :key="item.value" :value="item.value">
              {{ item.label }}
            </option>
          </select></label
        >
      </div>
      <MessageAdapterEditor
        ref="adapterEditor"
        v-model="state.config[lane.id]"
        :lane="lane.id"
        :api-mode="protocol"
        :types="lane.types"
      />
      <div class="adapter-actions">
        <button class="primary" :disabled="saving" @click="save">
          {{ saving ? '保存中…' : '保存消息适配' }}</button
        ><button :disabled="saving" @click="load">重新载入</button>
      </div>
    </template>
    <p v-if="error" class="danger" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
  </section>
</template>
<style scoped>
.adapter-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  margin: 16px 0;
}
.adapter-actions label {
  min-width: 180px;
  flex: 1;
}
</style>
