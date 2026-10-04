<script setup lang="ts">
import { useTimedError } from '../../composables/useTimedError';
import { onMounted, onUnmounted, ref } from 'vue';
import { api } from '../../lib/api';
const props = defineProps<{ settings: any; flush: () => Promise<unknown> }>();
const sessions = ref<any[]>([]),
  selectedSession = ref(''),
  indexes = ref<any>({ cached_entries: 0 });
const status = ref<any>({ models: {}, jobs: [] }),
  error = ref('');
const pending = ref<Record<string, boolean>>({}),
  feedback = ref(''),
  now = ref(Date.now());
const active = (job: any) => ['queued', 'running'].includes(job.status);
const busy = (purpose: string) =>
  !!pending.value[purpose] ||
  status.value.jobs.some(
    (j: any) =>
      active(j) &&
      (j.kind === `download:${purpose}` ||
        j.kind === `warmup:${purpose}` ||
        (purpose === 'embedding' && j.kind === 'index')),
  );
const rebuilding = () =>
  !!pending.value.rebuild ||
  status.value.jobs.some((j: any) => active(j) && ['rebuild', 'index'].includes(j.kind));
let timer: ReturnType<typeof setTimeout> | undefined,
  disposed = false;
async function refresh() {
  try {
    const [models, counts] = await Promise.all([api('/local-models'), api('/retrieval/indexes')]);
    status.value = models;
    indexes.value = counts;
    now.value = Date.now();
  } catch (e) {
    error.value = String(e);
  }
}
async function poll() {
  await refresh();
  if (!disposed) timer = setTimeout(poll, 1500);
}
function remember(job: any) {
  status.value.jobs = [...status.value.jobs.filter((j: any) => j.id !== job.id), job];
}
async function action(purpose: string, operation: string) {
  if (busy(purpose)) return;
  pending.value[purpose] = true;
  error.value = '';
  feedback.value = operation === 'download' ? '正在提交下载任务…' : '正在提交预热任务…';
  try {
    await props.flush();
    remember(await api(`/local-models/${purpose}/${operation}`, {}));
    feedback.value = '任务已提交，正在后台执行，可展开下方任务查看进度。';
  } catch (e) {
    error.value = String(e);
    feedback.value = '';
  } finally {
    pending.value[purpose] = false;
  }
}
async function rebuild() {
  if (!selectedSession.value || rebuilding()) return;
  pending.value.rebuild = true;
  error.value = '';
  feedback.value = '正在提交世界书索引任务…';
  try {
    await props.flush();
    remember(await api(`/sessions/${selectedSession.value}/retrieval/rebuild`, {}));
    feedback.value = '世界书索引正在后台准备，完成条目数会自动更新。';
  } catch (e) {
    error.value = String(e);
    feedback.value = '';
  } finally {
    pending.value.rebuild = false;
  }
}
async function cancel(id: string) {
  try {
    await api(`/local-models/jobs/${id}/cancel`, {});
    await refresh();
  } catch (e) {
    error.value = String(e);
  }
}
onMounted(async () => {
  await poll();
  try {
    sessions.value = await api('/sessions');
    selectedSession.value = sessions.value[0]?.id || '';
  } catch (e) {
    error.value = String(e);
  }
});
onUnmounted(() => {
  disposed = true;
  clearTimeout(timer);
});
useTimedError(error);
</script>
<template>
  <h3>本地检索模型</h3>
  <p class="muted">
    配置自动保存，下载和预热会等待保存完成。默认两个 0.6B 模型适合 12 GB 显卡；4B
    需要更多空闲显存。推理不可用时记录降级，不转 CPU。
  </p>
  <section v-for="purpose in ['embedding', 'reranker']" :key="purpose">
    <h4>{{ purpose === 'embedding' ? '向量模型' : '重排模型' }}</h4>
    <label
      >模型<select v-model="settings.local_models[purpose].model">
        <option
          v-for="size in ['0.6B', '4B']"
          :value="`Qwen/Qwen3-${purpose === 'embedding' ? 'Embedding' : 'Reranker'}-${size}`"
        >
          Qwen3 {{ size }}
        </option>
      </select></label
    >
    <label
      >量化<select v-model="settings.local_models[purpose].quantization">
        <option value="none">不量化（BF16）</option>
        <option value="int8">INT8（LLM.int8()）</option>
      </select></label
    >
    <p v-if="settings.local_models[purpose].quantization === 'int8'" class="muted">
      INT8 复用原始模型文件，预热时量化；切换量化方式后需重新预热。4B 建议从批量大小 1 开始。
    </p>
    <label>版本<input v-model="settings.local_models[purpose].revision" /></label>
    <label>设备<input v-model="settings.local_models[purpose].device" placeholder="cuda:0" /></label>
    <label
      >批量大小<input
        type="number"
        min="1"
        max="128"
        v-model.number="settings.local_models[purpose].batch_size"
    /></label>
    <label v-if="purpose === 'embedding'"
      >向量维度<input
        type="number"
        min="32"
        max="2560"
        v-model.number="settings.local_models[purpose].dimensions"
    /></label>
    <p class="muted">
      {{
        status.models[purpose]?.ready
          ? '已预热'
          : status.models[purpose]?.downloaded
            ? '已下载，待预热'
            : '尚未下载'
      }}
    </p>
    <div class="actions">
      <button :disabled="busy(purpose)" @click="action(purpose, 'download')">
        {{ busy(purpose) ? '后台任务执行中…' : '下载已保存的模型' }}
      </button>
      <button
        :disabled="busy(purpose) || !status.models[purpose]?.downloaded"
        @click="action(purpose, 'warmup')"
      >
        {{ busy(purpose) ? '请等待任务完成' : '预热' }}
      </button>
    </div>
  </section>
  <h3>世界书索引</h3>
  <p class="muted">
    全局已缓存
    {{ indexes.cached_entries }} 条向量条目（包含不同内容和配置版本）。仅启用向量通道时生成向量；关键词 / BM25
    准备完成不会增加此数。世界书修改后自动使用新内容；向量未就绪时先使用其他通道。
  </p>
  <p class="muted">
    向量缓存保存在本地数据库，重启后可复用，无固定有效期。同一数据目录内，内容和模型配置相同的条目可跨对话复用；
    条目、宏展开结果、模型版本或分块参数变化会使用新缓存。清理会移除不再被对话引用的版本，并按缓存容量上限淘汰。
    BM25 内存索引重启后会在正常发言时自动重建；Embedding 预热后，发言会自动在后台补建缺失向量。
    只读预览不建立索引。下方按钮用于提前准备，无需每次启动手动点击。
  </p>
  <label
    >待准备的对话<select v-model="selectedSession">
      <option v-for="s in sessions" :key="s.id" :value="s.id">
        {{ s.title || s.name }} · {{ s.persona }}
      </option>
    </select></label
  >
  <button :disabled="!selectedSession || rebuilding()" @click="rebuild">
    {{ rebuilding() ? '正在后台准备索引…' : '准备此对话的世界书索引' }}
  </button>
  <AppNotification :message="feedback" />
  <p
    v-for="job in status.jobs.filter((j: any) => active(j))"
    :key="job.id"
    role="status"
    :class="job.status === 'error' ? 'error' : 'muted'"
  >
    {{ job.kind }} ·
    {{
      (
        {
          queued: '排队中',
          running: '后台执行中',
          complete: '已完成',
          error: '失败',
          cancelled: '已取消',
        } as any
      )[job.status]
    }}
    {{ job.message || job.error
    }}<span v-if="job.total_entries !== undefined">
      · 已准备 {{ job.completed_entries }} / {{ job.total_entries }} 条<span
        v-if="active(job) && job.entry_started_at"
      >
        · 当前条目耗时 {{ Math.max(0, Math.floor(now / 1000 - job.entry_started_at)) }} 秒</span
      ></span
    >
  </p>
  <AppNotification :message="error" />
  <details v-if="status.jobs.length">
    <summary>下载与索引任务</summary>
    <div v-for="job in status.jobs" :key="job.id">
      <p>
        {{ job.kind }} ·
        {{
          (
            {
              queued: '排队中',
              running: '进行中',
              complete: '已完成',
              error: '失败',
              cancelled: '已取消',
            } as any
          )[job.status] || job.status
        }}<span v-if="job.phase === 'metadata'"> · 正在获取文件信息…</span
        ><span v-else-if="job.phase === 'cached'"> · 已使用本地模型，无需下载</span
        ><span v-else> · {{ Math.round(job.progress * 100) }}%</span>
      </p>
      <progress
        :value="
          job.status === 'running' && !job.total_bytes && job.kind.startsWith('download:')
            ? undefined
            : job.progress
        "
        max="1"
        aria-label="任务进度"
      />
      <p v-if="job.downloaded_bytes !== undefined">
        {{ (job.downloaded_bytes / 1048576).toFixed(1) }} MB<span v-if="job.total_bytes">
          / {{ (job.total_bytes / 1048576).toFixed(1) }} MB</span
        ><span v-if="job.status === 'running'">
          · {{ ((job.bytes_per_second || 0) / 1048576).toFixed(1) }} MB/s</span
        >
      </p>
      <AppNotification :message="job.error || ''" />
      <button v-if="['queued', 'running'].includes(job.status)" @click="cancel(job.id)">取消</button>
    </div>
  </details>
</template>
