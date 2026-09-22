<script setup lang="ts">
import { useTimedError } from '../../composables/useTimedError';
import { computed, onMounted, onBeforeUnmount, ref } from 'vue';
import { api } from '../../lib/api';

const props = defineProps<{ status: any }>();
defineEmits<{ close: []; settings: [] }>();
const models = props.status.enabled as string[];
const labels: Record<string, string> = { embedding: 'Embedding', reranker: 'Reranker' };
const jobs = ref<any[]>([]);
const pending = ref(false),
  submitted = ref(false),
  error = ref('');
const available = models.filter((purpose) => props.status.models[purpose].downloaded);
const missing = models.filter((purpose) => !props.status.models[purpose].downloaded);
const running = computed(() => jobs.value.some((job) => ['queued', 'running'].includes(job.status)));
let timer: ReturnType<typeof setTimeout> | undefined;
let disposed = false;
async function refresh() {
  try {
    const status = await api('/local-models');
    if (disposed) return;
    jobs.value = jobs.value.map((job) => status.jobs.find((item: any) => item.id === job.id) || job);
    if (running.value) timer = setTimeout(refresh, 1000);
  } catch (e) {
    if (!disposed) error.value = String(e);
  }
}
async function warmup() {
  if (pending.value || submitted.value) return;
  pending.value = true;
  error.value = '';
  const results = await Promise.allSettled(
    available.map((purpose) => api(`/local-models/${purpose}/warmup`, {})),
  );
  if (disposed) return;
  jobs.value = results.flatMap((result) => (result.status === 'fulfilled' ? [result.value] : []));
  error.value = results
    .flatMap((result) => (result.status === 'rejected' ? [String(result.reason)] : []))
    .join('\n');
  submitted.value = true;
  pending.value = false;
  if (jobs.value.length) await refresh();
}
onMounted(() => {
  localStorage.setItem('tavern-warmup-startup', props.status.startup_id);
});
onBeforeUnmount(() => {
  disposed = true;
  clearTimeout(timer);
});
useTimedError(error);
</script>

<template>
  <h2>预热本地检索模型？</h2>
  <p>
    你已启用
    {{
      models.map((purpose) => labels[purpose]).join('、')
    }}。服务刚启动，是否现在加载模型？预热会占用内存或显存。
  </p>
  <p v-if="missing.length" class="muted">
    {{ missing.map((purpose) => labels[purpose]).join('、') }}
    尚未下载，请先到设置中的“混合召回与本地模型”页面下载模型。
  </p>
  <p class="muted">暂不预热也可继续使用；不可用的检索或重排通道会降级。已保存的世界书向量缓存仍可复用。</p>
  <AppNotification v-for="job in jobs" :key="`error-${job.id}`" :message="job.error || ''" />
  <p
    v-for="job in jobs.filter((j: any) => j.status !== 'error')"
    :key="job.id"
    role="status"
    :class="{ error: job.status === 'error' }"
  >
    {{ labels[job.kind.split(':')[1]] }}：{{
      (
        {
          queued: '排队中',
          running: '预热中…',
          complete: '预热完成',
          error: '预热失败',
          cancelled: '已取消',
        } as any
      )[job.status]
    }}
    {{ job.error || '' }}
  </p>
  <AppNotification :message="error" />
  <div class="actions">
    <button v-if="!submitted && available.length" :disabled="pending" @click="warmup">
      {{ pending ? '正在提交…' : '现在预热' }}
    </button>
    <button :disabled="pending" @click="$emit('close')">
      {{ submitted ? (running ? '后台继续' : '完成') : '暂不预热' }}
    </button>
    <button :disabled="pending" @click="$emit('settings')">打开设置</button>
  </div>
</template>
