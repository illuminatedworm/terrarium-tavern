<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { api } from '../../lib/api';
import WorkData from '../records/WorkData.vue';

interface FailedRequest {
  id: string;
  created: number;
  model: string;
  purpose: string;
  error_type: string;
  bytes: number;
  request: { body: { truncated: boolean } };
  response: { status_code: number; body: { truncated: boolean; complete: boolean } } | null;
}
const records = ref<FailedRequest[]>([]);
const total = ref(0);
const offset = ref(0);
const busy = ref(false);
const error = ref('');
async function load() {
  busy.value = true;
  error.value = '';
  try {
    const result = await api(`/debug/failed-requests?offset=${offset.value}`);
    records.value = result.items;
    total.value = result.total;
  } catch (e: any) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
async function refresh() {
  offset.value = 0;
  await load();
}
async function turnPage(delta: number) {
  offset.value += delta * 100;
  await load();
}
onMounted(refresh);
</script>

<template>
  <h4>失败请求原始数据</h4>
  <p class="muted">
    沿用“存储与维护”的详细记录保留天数和容量上限，清理时保留最近 100
    条；运行中及可恢复记录受保护，可能暂时超过限额。 下载包包含 HTTP
    信息、请求正文及已接收的响应字节，保留原始换行和事件分隔。
  </p>
  <button type="button" :disabled="busy" @click="refresh">{{ busy ? '正在读取…' : '刷新失败记录' }}</button>
  <p v-if="error" role="alert" class="settings-field-error">{{ error }}</p>
  <p v-else-if="!busy && !records.length" class="muted">暂无失败请求记录。</p>
  <p v-if="total" class="muted">
    共 {{ total }} 条，当前显示第 {{ offset + 1 }} 至 {{ offset + records.length }} 条。
  </p>
  <div v-if="offset || offset + records.length < total" class="actions">
    <button type="button" :disabled="busy || !offset" @click="turnPage(-1)">上一页</button>
    <button type="button" :disabled="busy || offset + records.length >= total" @click="turnPage(1)">
      下一页
    </button>
  </div>
  <details v-for="record in records" :key="record.id" class="failed-request-record">
    <summary>
      {{ new Date(record.created * 1000).toLocaleString() }} · {{ record.model }} ·
      {{ record.response ? `HTTP ${record.response.status_code}` : '未收到 HTTP 响应' }} ·
      {{ record.error_type }}
    </summary>
    <p v-if="record.request.body.truncated || record.response?.body.truncated" class="muted">
      原始正文超出保留上限，下载包内已标记截断范围。
    </p>
    <p v-if="record.response && !record.response.body.complete" class="muted">
      响应未读取至结束，仅保留失败前已收到的字节。
    </p>
    <p>
      <a :href="`/api/debug/failed-requests/${record.id}`" download
        >下载原始数据（ZIP，{{ (record.bytes / 1024).toFixed(1) }} KiB）</a
      >
    </p>
    <WorkData :value="record" label="HTTP 信息" />
  </details>
</template>
