<script setup lang="ts">
import { useTimedError } from '../../composables/useTimedError';
import { onMounted, onUnmounted, ref } from 'vue';
import { api } from '../../lib/api';
defineProps<{ settings: any }>();
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
const parameters = [
  ['bm25_min_coverage', 'BM25 最低检索词覆盖率'],
  ['bm25_min_score', 'BM25 最低相对得分'],
  ['embedding_min_similarity', '向量最低相似度'],
  ['rerank_min_score', '重排最低相关性'],
  ['candidate_count', '每通道候选数'],
  ['fusion_count', '融合候选数'],
  ['rrf_k', 'RRF 平滑系数'],
  ['bm25_k1', 'BM25 k1'],
  ['bm25_b', 'BM25 b'],
  ['gram_weight', '字符二元组权重'],
  ['query_lookback', '线索回看数量'],
  ['query_chars', '线索长度上限（字符）'],
  ['input_chars', '用户输入检索上限（字符）'],
  ['chunk_tokens', '切片长度（神经 tokens / BM25 词元）'],
  ['chunk_overlap', '切片重叠（神经 tokens / BM25 词元）'],
  ['embedding_timeout', '向量查询超时（秒）'],
  ['rerank_timeout', '重排超时（秒）'],
];
useTimedError(error);
</script>
<template>
  <p class="muted">
    检索线索和输入上限以字符（Unicode 码点）计数；切片长度/重叠在神经通道以该本地模型 tokenizer 的 tokens
    计数，BM25 则以分词词元计数。它们不等同于正文模型的 token 预算。
  </p>
  <h3>召回方案</h3>
  <p class="muted">默认关键词＋BM25，无需神经模型。中英日分词由后端执行；混合方案按加权 RRF 合并。</p>
  <label
    v-for="[key, label] in [
      ['keyword', '关键词'],
      ['bm25', 'BM25'],
      ['embedding', 'Embedding 向量'],
    ]"
    :key="key"
    ><input type="checkbox" v-model="settings.retrieval.channels" :value="key" />{{ label }} · 融合权重<input
      aria-label="通道权重"
      type="number"
      min="0.01"
      max="100"
      step="0.1"
      v-model.number="settings.retrieval.weights[key]"
  /></label>
  <details>
    <summary>高级召回参数</summary>
    <label v-for="[key, label] in parameters" :key="key"
      >{{ label }}<input type="number" step="any" v-model.number="settings.retrieval[key]"
    /></label>
  </details>
  <label
    ><input type="checkbox" v-model="settings.local_models.reranker_enabled" />融合后使用本地 Reranker</label
  >
  <label
    >每条重排片段数<input
      type="number"
      min="1"
      max="8"
      step="1"
      v-model.number="settings.local_models.rerank_fragments"
  /></label>
  <p class="muted">
    每个世界书条目最多选取多少个相关片段交给 Reranker 分别评分，取最高分作为条目得分；默认 3 片，范围
    1–8。这不是最终注入的条目数，也不一定是正文开头的几片。增加片数可以扩大覆盖范围，但会增加推理开销。
  </p>
  <p class="muted">
    按模型的 token（分词单位）切片，不按行或 Markdown 段落切片。当前每片最多
    {{ settings.retrieval.chunk_tokens }} token，相邻片段重叠
    {{ settings.retrieval.chunk_overlap }} token；可在“高级召回参数”调整。 当前每条最多选
    {{ settings.local_models.rerank_fragments }} 片，合计约
    {{ settings.retrieval.chunk_tokens * settings.local_models.rerank_fragments }} token
    上限，含重叠内容及可能附带的标题、关键词；不足时按实际长度处理。 默认 512 token × 3 片 = 1536 token。Token
    不等于字符，不同语言、符号和分词器的换算不同，不能保证固定字数。
  </p>
  <h3>本地检索模型</h3>
  <p class="muted">
    先保存配置，再下载和预热。默认两个 0.6B 模型适合 12 GB 显卡；4B
    需要更多空闲显存。推理不可用时记录降级，不转 CPU。
  </p>
  <section v-for="purpose in ['embedding', 'reranker']" :key="purpose">
    <h4>{{ purpose === 'embedding' ? 'Embedding' : 'Reranker' }}</h4>
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
      <option v-for="s in sessions" :key="s.id" :value="s.id">{{ s.name }} · {{ s.persona }}</option>
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
