<script setup lang="ts">
import { useTimedError } from '../../composables/useTimedError';
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { api } from '../../lib/api';
import type { LocalModelPurpose, ModelWarmupState } from '../../composables/useModelWarmup';
const props = defineProps<{ settings: any; flush: () => Promise<unknown>; warmup: ModelWarmupState }>();
const purposes: Array<{ key: LocalModelPurpose; label: string; family: string }> = [
  { key: 'embedding', label: '向量模型', family: 'Embedding' },
  { key: 'reranker', label: '重排模型', family: 'Reranker' },
];
const sessions = ref<any[]>([]),
  selectedSession = ref(''),
  indexes = ref<any>({ cached_entries: 0 });
const status = ref<any>({ models: {}, jobs: [] }),
  error = ref('');
const isAndroid = computed(() => status.value.platform === 'android');
const modelFiles: Partial<Record<LocalModelPurpose, HTMLInputElement | null>> = {};
function rememberInput(purpose: LocalModelPurpose, element: unknown) {
  modelFiles[purpose] = element as HTMLInputElement | null;
}
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
const modelState = (purpose: string) => {
  if (status.value.available === false) return { label: '此平台不可用', tone: 'missing' };
  const model = status.value.models[purpose];
  if (model?.configuration_supported === false) return { label: '需选择手机配置', tone: 'missing' };
  return model?.ready
    ? { label: '已预热', tone: 'ready' }
    : model?.downloaded
      ? { label: isAndroid.value ? '已导入，待预热' : '已下载，待预热', tone: 'idle' }
      : { label: isAndroid.value ? '尚未导入' : '尚未下载', tone: 'missing' };
};
const statusLabels: Record<string, string> = {
  queued: '排队中',
  running: '进行中',
  complete: '已完成',
  error: '失败',
  cancelled: '已取消',
};
function jobLabel(kind: string) {
  const [operation, purpose] = kind.split(':');
  const target = purposes.find((item) => item.key === purpose)?.label;
  const name = ({ download: '下载', warmup: '预热', index: '世界书索引', rebuild: '世界书索引' } as any)[
    operation
  ];
  return name ? (target ? `${target}${name}` : name) : kind;
}
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
async function download(purpose: LocalModelPurpose) {
  if (busy(purpose)) return;
  pending.value[purpose] = true;
  error.value = '';
  feedback.value = '正在提交下载任务…';
  try {
    await props.flush();
    remember(await api(`/local-models/${purpose}/download`, {}));
    feedback.value = '下载已在后台开始，进度见下方任务。';
  } catch (e) {
    error.value = String(e);
    feedback.value = '';
  } finally {
    pending.value[purpose] = false;
  }
}
async function chooseMobileConfig(purpose: LocalModelPurpose) {
  if (busy(purpose)) return;
  pending.value[purpose] = true;
  error.value = '';
  try {
    props.settings.local_models[purpose] = { ...status.value.models[purpose].suggested_config };
    await props.flush();
    await refresh();
    feedback.value = '手机 CPU/Q8_0 配置已保存，请导入下方指定的 GGUF 文件。';
  } catch (e) {
    error.value = String(e);
  } finally {
    pending.value[purpose] = false;
  }
}
async function importModel(purpose: LocalModelPurpose, event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file || busy(purpose)) return;
  pending.value[purpose] = true;
  error.value = '';
  feedback.value = '正在传入并校验模型文件…';
  try {
    await props.flush();
    const body = new FormData();
    body.append('file', file);
    await api(`/local-models/${purpose}/import`, body);
    await refresh();
    feedback.value = '模型已导入并通过 SHA-256 校验，可以预热。';
  } catch (e) {
    error.value = String(e);
    feedback.value = '';
  } finally {
    input.value = '';
    pending.value[purpose] = false;
  }
}
// Warmup goes through the app-wide observer, the same path as the startup prompt, so completion
// or failure is announced even after leaving this page.
async function warm(purpose: LocalModelPurpose) {
  if (busy(purpose)) return;
  pending.value[purpose] = true;
  error.value = '';
  feedback.value = '正在提交预热任务…';
  try {
    await props.flush();
    const accepted = await props.warmup.start([purpose]);
    accepted.forEach(remember);
    feedback.value = accepted.length ? '预热已在后台开始，完成后会通知。' : '';
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
  <h4>检索模型</h4>
  <p v-if="status.available === false" role="status">{{ status.reason }}</p>
  <p v-else-if="isAndroid" class="muted">
    手机使用 CPU 与 Q8_0 GGUF，配置自动保存。先选择手机配置、导入对应文件，再预热。模型权重独立于 APK；
    重排多条候选可能较慢，可在所用模式的检索设置中调整超时。不会自动改变已有 CUDA 配置。
  </p>
  <p v-else class="muted">
    配置自动保存，下载和预热会等待保存完成。默认两个 0.6B 模型适合 12 GB 显卡，4B
    需要更多空闲显存；推理不可用时记录降级，不转 CPU。
  </p>
  <div class="local-model-grid">
    <section
      v-for="purpose in purposes"
      :key="purpose.key"
      class="local-model-card"
      :aria-label="purpose.label"
    >
      <header>
        <strong>{{ purpose.label }}</strong>
        <span :class="['local-model-state', modelState(purpose.key).tone]" role="status">{{
          modelState(purpose.key).label
        }}</span>
      </header>
      <div class="local-model-fields">
        <label
          >模型<select v-model="settings.local_models[purpose.key].model">
            <option
              v-for="size in ['0.6B', '4B']"
              :key="size"
              :value="`Qwen/Qwen3-${purpose.family}-${size}`"
            >
              Qwen3 {{ size }}
            </option>
            <!-- Desktop lists Android-only choices only when a saved value already uses them. -->
            <option
              v-if="
                purpose.key === 'embedding' &&
                (isAndroid || settings.local_models.embedding.model === 'Qwen/Qwen3-Embedding-0.6B-GGUF')
              "
              value="Qwen/Qwen3-Embedding-0.6B-GGUF"
            >
              Qwen3 0.6B 官方 GGUF（Android）
            </option>
          </select></label
        >
        <label
          >量化<select v-model="settings.local_models[purpose.key].quantization">
            <option value="none">不量化（BF16）</option>
            <option value="int8">INT8（LLM.int8()）</option>
            <option
              v-if="isAndroid || settings.local_models[purpose.key].quantization === 'q8_0'"
              value="q8_0"
            >
              Q8_0 GGUF（Android CPU）
            </option>
          </select></label
        >
        <label
          >设备<input
            v-model="settings.local_models[purpose.key].device"
            :placeholder="isAndroid ? 'cpu' : 'cuda:0'"
        /></label>
        <label
          >批量大小<input
            v-model.number="settings.local_models[purpose.key].batch_size"
            type="number"
            min="1"
            max="128"
        /></label>
        <label>版本<input v-model="settings.local_models[purpose.key].revision" /></label>
        <label v-if="purpose.key === 'embedding'"
          >向量维度<input
            v-model.number="settings.local_models[purpose.key].dimensions"
            type="number"
            min="32"
            max="2560"
        /></label>
      </div>
      <p v-if="settings.local_models[purpose.key].quantization === 'int8'" class="muted">
        INT8 复用原始模型文件，预热时量化；切换量化方式后需重新预热。4B 建议从批量大小 1 开始。
      </p>
      <template v-if="isAndroid">
        <p v-if="status.models[purpose.key]?.configuration_error" role="status">
          {{ status.models[purpose.key].configuration_error }}
        </p>
        <p class="muted">
          权重来源：{{ status.models[purpose.key]?.source_label }}。文件：
          {{ status.models[purpose.key]?.filename }}（约
          {{ Math.round((status.models[purpose.key]?.bytes || 0) / 1048576) }} MiB）。 运行方式：{{
            status.engine
          }}，{{ status.cpu_threads }} 个 CPU 线程。
        </p>
        <button
          :disabled="status.available === false || busy(purpose.key)"
          @click="chooseMobileConfig(purpose.key)"
        >
          使用手机 CPU/Q8_0 配置
        </button>
      </template>
      <div class="actions">
        <button
          v-if="!isAndroid"
          :disabled="status.available === false || busy(purpose.key)"
          @click="download(purpose.key)"
        >
          下载已保存的模型
        </button>
        <template v-else>
          <input
            hidden
            :ref="(element) => rememberInput(purpose.key, element)"
            type="file"
            accept=".gguf"
            :aria-label="'导入' + purpose.label"
            @change="importModel(purpose.key, $event)"
          />
          <button
            :disabled="
              status.available === false ||
              busy(purpose.key) ||
              !status.models[purpose.key]?.configuration_supported
            "
            @click="modelFiles[purpose.key]?.click()"
          >
            导入 GGUF
          </button>
        </template>
        <button
          :disabled="
            status.available === false || busy(purpose.key) || !status.models[purpose.key]?.downloaded
          "
          @click="warm(purpose.key)"
        >
          预热
        </button>
        <small v-if="busy(purpose.key)">后台任务执行中…</small>
      </div>
    </section>
  </div>
  <h4>世界书索引</h4>
  <p class="muted">
    全局已缓存 {{ indexes.cached_entries }} 条向量条目（包含不同内容和配置版本）。Embedding
    预热后，发言时会在后台补建缺失向量；下方按钮仅用于提前准备，无需每次启动手动点击。
  </p>
  <div class="local-index-row">
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
  </div>
  <details class="settings-advanced">
    <summary>索引与缓存说明</summary>
    <p class="muted">
      仅启用向量通道时生成向量；关键词 / BM25
      准备完成不会增加缓存条数。世界书修改后自动使用新内容；向量未就绪时先使用其他通道。
    </p>
    <p class="muted">
      向量缓存保存在本地数据库，重启后可复用，无固定有效期。同一数据目录内，内容和模型配置相同的条目可跨对话复用；
      条目、宏展开结果、模型版本或分块参数变化会使用新缓存。清理会移除不再被对话引用的版本，并按缓存容量上限淘汰。
      BM25 内存索引重启后会在正常发言时自动重建。只读预览不建立索引。
    </p>
  </details>
  <AppNotification :message="feedback" />
  <AppNotification :message="error" />
  <details v-if="status.jobs.length" class="settings-advanced local-jobs" :open="status.jobs.some(active)">
    <summary>
      后台任务（{{ status.jobs.filter(active).length }} 进行中 / 共 {{ status.jobs.length }}）
    </summary>
    <div v-for="job in status.jobs" :key="job.id" class="local-job">
      <p>
        <strong>{{ jobLabel(job.kind) }}</strong> · {{ statusLabels[job.status] || job.status
        }}<span v-if="job.phase === 'metadata'"> · 正在获取文件信息…</span
        ><span v-else-if="job.phase === 'cached'"> · 已使用本地模型，无需下载</span
        ><span v-else-if="job.total_entries !== undefined">
          · 已准备 {{ job.completed_entries }} / {{ job.total_entries }} 条<span
            v-if="active(job) && job.entry_started_at"
          >
            · 当前条目耗时 {{ Math.max(0, Math.floor(now / 1000 - job.entry_started_at)) }} 秒</span
          ></span
        ><span v-else> · {{ Math.round(job.progress * 100) }}%</span
        ><span v-if="job.downloaded_bytes !== undefined">
          · {{ (job.downloaded_bytes / 1048576).toFixed(1) }} MB<span v-if="job.total_bytes">
            / {{ (job.total_bytes / 1048576).toFixed(1) }} MB</span
          ><span v-if="job.status === 'running'">
            · {{ ((job.bytes_per_second || 0) / 1048576).toFixed(1) }} MB/s</span
          ></span
        >
        <span v-if="job.message"> · {{ job.message }}</span>
      </p>
      <div class="local-job-progress">
        <progress
          :value="
            job.status === 'running' && !job.total_bytes && job.kind.startsWith('download:')
              ? undefined
              : job.progress
          "
          max="1"
          aria-label="任务进度"
        />
        <button v-if="active(job)" class="quiet" @click="cancel(job.id)">取消</button>
      </div>
      <AppNotification :message="job.error || ''" />
    </div>
  </details>
</template>
