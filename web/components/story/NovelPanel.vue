<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { rawApi } from '../../lib/api';
import {
  novelChanges,
  novelConnectionError,
  novelConnectionEpoch,
  retryNovelActivity,
  novelStatus,
  type NovelSource,
  type NovelState,
  type NovelJob,
  type NovelTrace,
} from '../../lib/novel';
import Icon from '../common/Icon.vue';
import type { Provider } from '../../lib/settingsDocument';

const props = defineProps<{ sessionId: string; locked: boolean; providers: Provider[] }>();
function providerName(id: string) {
  if (!id || id.startsWith('unconfigured-')) return '未配置';
  return props.providers.find((provider) => provider.id === id)?.name || '原连接已移除';
}
const emit = defineEmits<{
  settings: [];
  changed: [value: { enabled: boolean; active: number }];
  stopped: [];
}>();
const state = ref<NovelState | null>(null),
  error = ref(''),
  busy = ref('');
const encoding = ref('utf-8'),
  fileInput = ref<HTMLInputElement>();
const expanded = ref(''),
  records = ref<NovelTrace[]>([]),
  more = ref(false);
const reports = ref<Array<{ kind: string; content: string }>>([]),
  detailExpired = ref(false);
const olderJobs = ref<NovelJob[]>([]),
  olderCursor = ref<number | null | undefined>();
const jobs = computed(() =>
  [...new Map([...olderJobs.value, ...(state.value?.jobs || [])].map((job) => [job.id, job])).values()].sort(
    (a, b) => Number(b.ref.slice(1)) - Number(a.ref.slice(1)),
  ),
);
const archiveBefore = computed(() =>
  olderCursor.value === undefined ? state.value?.archive_before : olderCursor.value,
);
const excerpt = ref<{ text: string; start: number; end: number; has_more: boolean } | null>(null);
const viewing = ref<NovelSource | null>(null),
  history = ref(false);
let disposed = false,
  generation = 0,
  loading = false,
  reload = false;
const root = computed(() => `/sessions/${props.sessionId}/novel`);
const fileLocked = computed(() => props.locked || !!state.value?.active || !!busy.value);
function accept(value: NovelState) {
  if (state.value && value.event_seq < state.value.event_seq) return;
  if (olderCursor.value !== undefined && state.value) {
    olderJobs.value = [
      ...new Map(
        [...olderJobs.value, ...state.value.jobs.filter((job) => job.status === 'destroyed')].map((job) => [
          job.id,
          job,
        ]),
      ).values(),
    ];
  }
  state.value = value;
  emit('changed', { enabled: value.enabled, active: value.active });
}
async function load() {
  if (loading) {
    reload = true;
    return;
  }
  loading = true;
  try {
    do {
      reload = false;
      const value = await rawApi(root.value);
      if (!disposed) accept(value);
    } while (reload && !disposed);
  } catch (e) {
    if (!disposed) error.value = String(e);
  } finally {
    loading = false;
    if (reload && !disposed) void load();
  }
}
async function action(name: string, run: () => Promise<NovelState>) {
  if (busy.value) return;
  busy.value = name;
  error.value = '';
  try {
    const value = await run();
    if (!disposed) accept(value);
  } catch (e) {
    if (!disposed) error.value = String(e);
  } finally {
    if (!disposed) busy.value = '';
  }
}
async function importSource(event: Event) {
  const input = event.target as HTMLInputElement,
    file = input.files?.[0];
  if (!file || !state.value) return;
  if (file.size > 64 * 1024 * 1024) {
    error.value = '原著文件不能超过 64 MiB';
    input.value = '';
    return;
  }
  const query = new URLSearchParams({
    filename: file.name,
    encoding: encoding.value,
    expected_revision: String(state.value.revision),
  });
  await action('import', async () => {
    const response = await fetch(`/api${root.value}/source?${query}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: file,
    });
    const result = await response.json();
    if (!response.ok)
      throw Error(typeof result.detail === 'string' ? result.detail : JSON.stringify(result.detail));
    return result;
  });
  input.value = '';
  excerpt.value = viewing.value = null;
}
async function authorize(enabled: boolean, unbind = false) {
  if (!state.value) return;
  await action('source', () =>
    rawApi(root.value, { expected_revision: state.value!.revision, enabled, unbind }, 'PATCH'),
  );
}
async function control(ref: string, operation: string) {
  await action(ref, () => rawApi(`${root.value}/jobs/${ref}/${operation}`, {}));
}
async function stopAll() {
  await action('stop-all', () => rawApi(`${root.value}/stop-all`, {}));
  emit('stopped');
}
async function details(ref: string, append = false) {
  const token = ++generation;
  expanded.value = ref;
  if (!append) {
    records.value = [];
    reports.value = [];
    detailExpired.value = false;
  }
  const after = append ? records.value.at(-1)?.seq || 0 : 0;
  try {
    const value = await rawApi(`${root.value}/jobs/${ref}/trace?after=${after}`);
    if (disposed || token !== generation) return;
    records.value = append ? [...records.value, ...value.records] : value.records;
    more.value = value.has_more;
    reports.value = value.reports;
    detailExpired.value = value.detail_expired;
  } catch (e) {
    if (!disposed && token === generation) error.value = String(e);
  }
}
async function preview(source: NovelSource, start = 0) {
  const token = ++generation;
  viewing.value = source;
  excerpt.value = null;
  try {
    const value = await rawApi(`${root.value}/sources/${source.id}/text?start=${start}`);
    if (!disposed && token === generation) excerpt.value = value;
  } catch (e) {
    if (!disposed && token === generation) error.value = String(e);
  }
}
async function loadOlder() {
  if (!archiveBefore.value) return;
  try {
    const value = await rawApi(`${root.value}/jobs?before=${archiveBefore.value}`);
    if (disposed) return;
    olderJobs.value = [...olderJobs.value, ...value.jobs];
    olderCursor.value = value.archive_before;
  } catch (e) {
    if (!disposed) error.value = String(e);
  }
}
watch(() => novelChanges.value[props.sessionId], load, { immediate: true });
watch(novelConnectionEpoch, () => {
  state.value = null;
  void load();
});
onBeforeUnmount(() => {
  disposed = true;
  generation++;
});
</script>

<template>
  <section class="novel-panel" aria-label="原著参考与调查">
    <div class="scene-heading">
      <h2>原著参考</h2>
      <button class="quiet" @click="emit('settings')"><Icon name="sliders" :size="16" />模型与额度</button>
    </div>
    <p v-if="novelConnectionError" role="status" class="muted">
      {{ novelConnectionError }} <button class="quiet" @click="retryNovelActivity">重新连接</button>
    </p>
    <p v-if="error" role="alert" class="error">
      {{ error }} <button class="quiet" @click="load">刷新重试</button>
    </p>
    <p v-if="!state" role="status">正在读取原著设置…</p>
    <template v-else>
      <p class="inspector-note">
        每个分支只绑定一个纯文本文件。调查者独立查阅，主 agent 接收报告；原著不会覆盖本局剧情。
      </p>
      <div v-if="state.source" class="novel-source">
        <strong>{{ state.source.name }}</strong>
        <p class="muted">
          {{ state.source.characters.toLocaleString() }} 字符 · {{ state.source.encoding }} ·
          {{ new Date(state.source.created * 1000).toLocaleString() }}
        </p>
        <p class="muted novel-path">副本位置：{{ state.source.location }}</p>
        <p v-if="state.source.missing" role="alert">
          参考文件缺失，调查无法读取。请先销毁调查者，再重新导入。
        </p>
        <div class="novel-actions">
          <button :disabled="state.source.missing" @click="preview(state.source)">查看副本</button>
          <a v-if="!state.source.missing" :href="`/api${root}/sources/${state.source.id}/download`" download
            >下载原文件</a
          >
          <button :disabled="fileLocked" @click="authorize(false, true)">解绑</button>
        </div>
      </div>
      <label
        >文件编码<select v-model="encoding" :disabled="fileLocked">
          <option value="utf-8">UTF-8（默认，识别 BOM）</option>
          <option value="utf-16">UTF-16</option>
          <option value="gb18030">GB18030</option>
          <option value="big5">Big5</option>
        </select></label
      >
      <input ref="fileInput" type="file" accept=".txt,.md,.markdown" hidden @change="importSource" />
      <button :disabled="fileLocked" @click="fileInput?.click()">
        <Icon name="import" :size="16" />{{
          busy === 'import' ? '正在导入…' : state.source ? '替换文件' : '导入原著文件'
        }}
      </button>
      <p class="muted">
        支持 txt、md、markdown，最大 64 MiB。保存固定副本，外部原文件修改不会自动生效；导入后需明确开启。
      </p>
      <label class="settings-check"
        ><span>允许当前分支的 Full Agent 委派查阅</span
        ><input
          type="checkbox"
          :checked="state.enabled"
          :disabled="fileLocked || !state.source || state.source.missing"
          @change="authorize(($event.target as HTMLInputElement).checked)"
      /></label>
      <p class="muted">开启后，选中文件的相关片段将发送给下方显示的模型服务。每次调查无需再次确认。</p>
      <p v-if="state.active" role="status" class="muted">
        {{ state.active }} 个调查者尚未销毁，暂不能替换、解绑或停用小说。已完成和已暂停的调查者也需销毁。
      </p>
      <p class="muted">
        新调查者：{{ state.effective.follows_main ? '跟随 Full Agent' : '用户指定模型' }} ·
        {{ providerName(state.effective.profile.provider_id) }} /
        {{ state.effective.profile.model || '尚未配置' }} ·
        {{ state.effective.channel === 'chatgpt' ? 'GPT 渠道，云端压缩' : '普通 API，摘要压缩' }}
      </p>
      <p v-if="state.effective.follows_main" class="muted">
        实际创建时跟随委派主 agent 的运行配置，包括本轮临时思考档位；已启动主生成的配置不受中途全局修改影响。
      </p>
      <div v-if="state.history.length > 1 || (!state.source && state.history.length)" class="novel-history">
        <button class="quiet" @click="history = !history">
          {{ history ? '收起' : '查看' }}历史文件版本（{{ state.history.length }}）
        </button>
        <div v-if="history" v-for="source in state.history" :key="source.id">
          <button class="quiet" :disabled="source.missing" @click="preview(source)">
            {{ source.name }} · {{ new Date(source.created * 1000).toLocaleString() }} ·
            {{ source.current ? '当前版本' : '历史版本，仅供回查' }}{{ source.missing ? ' · 缺失' : '' }}
          </button>
        </div>
      </div>
      <div v-if="viewing && excerpt" class="novel-excerpt">
        <div class="novel-actions">
          <strong>{{ viewing.name }} · {{ excerpt.start }}–{{ excerpt.end }} 字符</strong
          ><button class="quiet" @click="viewing = null">关闭</button>
        </div>
        <pre>{{ excerpt.text }}</pre>
        <button v-if="excerpt.has_more" @click="preview(viewing!, excerpt.end)">下一段</button>
      </div>
      <div class="scene-section-heading">
        <h3>原著调查者 · {{ state.active }}/3</h3>
        <button :disabled="!!busy || (!state.active && !locked)" @click="stopAll">
          <Icon name="stop" :size="16" />全部停止
        </button>
      </div>
      <p class="muted">
        每轮最多启动五次。主生成与调查独立运行，可跨轮；主 agent
        应在完成必要追问后积极销毁调查者。停止保留进度，销毁释放名额。
      </p>
      <p v-if="!state.jobs.length" class="muted">尚无调查。开启参考后，由主 agent 按需委派。</p>
      <article v-for="job in jobs" :key="job.id" class="novel-job">
        <div class="novel-actions">
          <strong>{{ job.ref }}</strong
          ><span>{{ novelStatus[job.status] }}</span>
        </div>
        <p>{{ job.task }}</p>
        <p v-if="job.pending_messages" class="muted">
          {{ job.pending_messages }} 条调查消息待主 agent 下一次请求接收；空闲时不会自动唤醒。
        </p>
        <p v-if="job.delivered_messages" class="muted">
          已送达主 agent {{ job.delivered_messages }} 条调查消息。
        </p>
        <p class="muted">
          发起于 H{{ job.round_position }} · {{ new Date(job.created * 1000).toLocaleString() }}
        </p>
        <p class="muted">
          固定模型：{{ providerName(job.config.profile.provider_id) }} / {{ job.config.profile.model }} ·
          {{ job.config.channel === 'chatgpt' ? 'GPT' : 'API'
          }}{{ job.config.follows_main ? '（创建时跟随主模型）' : '（用户指定）' }}
        </p>
        <p class="muted">
          请求 {{ job.requests }}/{{ job.config.max_requests }} · 累计运行 {{ job.elapsed.toFixed(1) }}/{{
            job.config.timeout_seconds
          }}
          秒
        </p>
        <p v-if="job.usage.tokens !== undefined" class="muted">
          累计实测输入 {{ job.usage.tokens.toLocaleString() }} tokens · 输出
          {{ job.usage.output_tokens?.toLocaleString() ?? '未知' }}
        </p>
        <p v-if="job.error" role="status" class="error">{{ job.error }}</p>
        <div class="novel-actions">
          <button v-if="job.status === 'running'" :disabled="!!busy" @click="control(job.ref, 'pause')">
            停止调查
          </button>
          <button
            v-if="job.status === 'paused' || job.status === 'failed'"
            :disabled="
              !!busy || job.requests >= job.config.max_requests || job.elapsed >= job.config.timeout_seconds
            "
            @click="control(job.ref, 'resume')"
          >
            恢复调查
          </button>
          <button v-if="job.status !== 'destroyed'" :disabled="!!busy" @click="control(job.ref, 'destroy')">
            销毁
          </button>
          <button class="quiet" @click="expanded === job.ref ? (expanded = '') : details(job.ref)">
            {{ expanded === job.ref ? '收起过程' : '查看完整过程' }}
          </button>
        </div>
        <div v-if="expanded === job.ref" class="novel-trace">
          <p v-if="job.background" class="muted">委派时背景：{{ job.background }}</p>
          <div v-for="(report, index) in reports" :key="index">
            <strong>{{
              report.kind === 'report'
                ? '调查报告'
                : report.kind === 'question'
                  ? '向主 agent 提问'
                  : '调查未完成'
            }}</strong>
            <pre>{{ report.content }}</pre>
          </div>
          <p v-if="detailExpired" class="muted">
            已销毁调查者的详细过程已按日志保留设置清理；报告与剧情历史保留。
          </p>
          <button class="quiet" @click="details(job.ref)">刷新过程</button>
          <div v-for="record in records" :key="record.seq">
            <strong>{{
              record.message._novel_compaction
                ? '上下文压缩'
                : record.message.role === 'tool'
                  ? '工具结果'
                  : record.message.role === 'assistant'
                    ? '调查者'
                    : '委派者消息'
            }}</strong>
            <pre>{{ record.message.content }}</pre>
            <pre v-if="record.message.tool_calls">{{
              JSON.stringify(record.message.tool_calls, null, 2)
            }}</pre>
          </div>
          <button v-if="more" @click="details(job.ref, true)">加载后续记录</button>
        </div>
      </article>
      <button v-if="archiveBefore" class="quiet" @click="loadOlder">加载更早的调查记录</button>
    </template>
  </section>
</template>
