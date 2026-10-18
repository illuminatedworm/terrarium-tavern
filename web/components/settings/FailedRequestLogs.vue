<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { api } from '../../lib/api';
import WorkData from '../records/WorkData.vue';
import Icon from '../common/Icon.vue';

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
const notice = ref('');
const confirming = ref(false);
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
async function clear() {
  busy.value = true;
  error.value = '';
  try {
    const result = await api('/debug/failed-requests', undefined, 'DELETE');
    notice.value = result.protected
      ? `已清空 ${result.removed} 条；${result.protected} 条属于运行中或可恢复的任务，暂时保留。`
      : `已清空 ${result.removed} 条。`;
  } catch (e: any) {
    error.value = e.message;
  } finally {
    confirming.value = false;
    busy.value = false;
  }
  if (!error.value) await refresh();
}
async function turnPage(delta: number) {
  offset.value += delta * 100;
  await load();
}
onMounted(refresh);
</script>

<template>
  <section class="failed-requests" aria-label="失败请求记录">
    <div class="failed-requests-heading">
      <h4>失败请求记录</h4>
      <small v-if="total">{{ total }} 条</small>
      <button
        type="button"
        class="icon-button"
        aria-label="刷新失败记录"
        title="刷新失败记录"
        :disabled="busy"
        @click="refresh"
      >
        <Icon name="refresh" :size="16" />
      </button>
      <template v-if="confirming">
        <button type="button" class="danger" :disabled="busy" @click="clear">确认清空</button>
        <button type="button" class="quiet" :disabled="busy" @click="confirming = false">取消</button>
      </template>
      <button
        v-else
        type="button"
        class="quiet"
        :disabled="busy || !total"
        @click="
          notice = '';
          confirming = true;
        "
      >
        <Icon name="trash" :size="15" />清空
      </button>
    </div>
    <p v-if="confirming" class="failed-requests-confirm" role="alert">
      将删除
      {{ total }} 条记录，删除后无法恢复。运行中或可恢复任务的记录会保留；剧情、工作记录和其他日志不受影响。
    </p>
    <p class="muted">
      最多保留最近 100
      条，并按“存储与维护”中详细记录的保留天数和容量清理；运行中与可恢复的记录受保护，可能暂时超出限额。下载包含
      HTTP 信息、请求正文和已收到的响应字节，保留原始换行与事件分隔。
    </p>
    <p v-if="notice" class="inline-feedback" role="status">{{ notice }}</p>
    <p v-if="error" role="alert" class="settings-field-error">{{ error }}</p>
    <p v-else-if="busy && !records.length" class="failed-requests-empty">正在读取…</p>
    <p v-else-if="!records.length" class="failed-requests-empty">暂无失败请求记录。</p>
    <div v-if="records.length" class="failed-request-list">
      <details v-for="record in records" :key="record.id" class="failed-request-record">
        <summary>
          <Icon name="chevron-right" :size="14" class="failed-request-caret" />
          <time>{{ new Date(record.created * 1000).toLocaleString() }}</time>
          <span class="failed-request-model">{{ record.model }}</span>
          <span class="failed-request-status" :class="{ silent: !record.response }">{{
            record.response ? `HTTP ${record.response.status_code}` : '未收到响应'
          }}</span>
          <span class="failed-request-error">{{ record.error_type }}</span>
        </summary>
        <div class="failed-request-body">
          <p v-if="record.request.body.truncated || record.response?.body.truncated" class="muted">
            原始正文超出保留上限，下载包内已标记截断范围。
          </p>
          <p v-if="record.response && !record.response.body.complete" class="muted">
            响应未读取至结束，仅保留失败前已收到的字节。
          </p>
          <a class="failed-request-download" :href="`/api/debug/failed-requests/${record.id}`" download
            ><Icon name="import" :size="15" />下载原始数据（ZIP，{{
              (record.bytes / 1024).toFixed(1)
            }}
            KiB）</a
          >
          <WorkData :value="record" label="HTTP 信息" />
        </div>
      </details>
    </div>
    <div v-if="total > records.length" class="failed-requests-pager">
      <small>第 {{ offset + 1 }}–{{ offset + records.length }} 条</small>
      <button type="button" class="quiet" :disabled="busy || !offset" @click="turnPage(-1)">上一页</button>
      <button
        type="button"
        class="quiet"
        :disabled="busy || offset + records.length >= total"
        @click="turnPage(1)"
      >
        下一页
      </button>
    </div>
  </section>
</template>
