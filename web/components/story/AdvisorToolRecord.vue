<script setup lang="ts">
import { ref, shallowRef } from 'vue';
import { rawApi } from '../../lib/api';
import { toolLabels, type AdvisorTrace } from '../../lib/advisor';
import AdvisorIcon from './AdvisorIcon.vue';

const props = defineProps<{ sessionId: string; seq: number; count: number }>();
const trace = shallowRef<AdvisorTrace>(),
  loading = ref(false),
  error = ref('');
async function load() {
  if (trace.value || loading.value) return;
  loading.value = true;
  error.value = '';
  try {
    trace.value = await rawApi(`/sessions/${props.sessionId}/advisor/exchanges/${props.seq}/trace`);
  } catch (e) {
    error.value = String(e);
  } finally {
    loading.value = false;
  }
}
</script>
<template>
  <details class="advisor-tool-record" @toggle="($event.currentTarget as HTMLDetailsElement).open && load()">
    <summary>
      <AdvisorIcon name="document" />已查阅与处理 {{ count }} 项<AdvisorIcon name="chevron" />
    </summary>
    <p v-if="loading" role="status">正在读取工具记录…</p>
    <p v-if="error" role="alert">{{ error }} <button @click="load">重试</button></p>
    <div v-for="(message, i) in trace" :key="i">
      <p v-for="call in message.tool_calls" :key="call.id">
        {{ toolLabels[call.function.name] || call.function.name }}
      </p>
      <pre v-if="message.role === 'tool'">{{ message.content }}</pre>
    </div>
  </details>
</template>
