<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { rawApi } from '../../lib/api';
import { channelLabels } from '../../lib/channels';
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
import AppModal from '../common/AppModal.vue';
import FieldHelp from '../common/FieldHelp.vue';
import Icon from '../common/Icon.vue';
import WebSources from '../common/WebSources.vue';
import type { ModelProfile, Provider } from '../../lib/settingsDocument';

const props = defineProps<{
  sessionId: string;
  locked: boolean;
  providers: Provider[];
  advisorDefault: ModelProfile | null;
}>();
function providerName(id: string) {
  if (!id || id.startsWith('unconfigured-')) return '未配置';
  return props.providers.find((provider) => provider.id === id)?.name || '原连接已移除';
}
const emit = defineEmits<{
  settings: [];
  services: [];
  advisor: [];
  changed: [value: { enabled: boolean; active: number }];
  stopped: [];
}>();
const state = ref<NovelState | null>(null),
  error = ref(''),
  busy = ref('');
const guidance = ref(''),
  savedGuidance = ref('');
const guidanceDirty = computed(() => guidance.value !== savedGuidance.value);
const advisorModel = computed(() =>
  props.advisorDefault ? state.value?.advisor_model_selection || props.advisorDefault : null,
);
const encoding = ref('utf-8'),
  fileInput = ref<HTMLInputElement>();
const expanded = ref(''),
  records = ref<NovelTrace[]>([]),
  more = ref(false);
const reports = ref<Array<{ kind: string; content: string }>>([]),
  detailExpired = ref(false);
const sourceNames = computed(() => new Map(state.value?.history.map((source) => [source.id, source.name])));
function jobSources(job: NovelJob) {
  return [job.source && sourceNames.value.get(job.source), job.web_enabled && '网络查询']
    .filter(Boolean)
    .join(' · ');
}
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
  history = ref(false),
  choosingFile = ref(false);
const excerptDialog = ref<HTMLElement>();
watch(
  excerptDialog,
  (dialog, _, onCleanup) => {
    if (!dialog) return;
    // Escape should close the file preview before it reaches the mobile drawer.
    const trigger = document.activeElement;
    dialog.querySelector<HTMLButtonElement>('button.close')?.focus();
    onCleanup(() => {
      if (trigger instanceof HTMLElement && trigger.isConnected) trigger.focus({ preventScroll: true });
    });
  },
  { flush: 'post' },
);
const statusTone: Record<NovelJob['status'], string> = {
  running: 'live',
  waiting: 'live',
  completed: 'done',
  paused: '',
  failed: 'warn',
  destroyed: '',
};
function when(seconds: number) {
  return new Date(seconds * 1000).toLocaleString(undefined, {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}
function percent(value: number, limit: number) {
  return `${Math.min(100, (value / limit) * 100)}%`;
}
// The server reports elapsed only when something changes; a running investigator's clock
// advances locally from the moment its state arrived, and the next state corrects it.
const received = ref(0),
  now = ref(0);
function elapsed(job: NovelJob) {
  return job.status === 'running'
    ? Math.min(job.config.timeout_seconds, job.elapsed + Math.max(0, now.value - received.value) / 1000)
    : job.elapsed;
}
let disposed = false,
  generation = 0,
  loading = false,
  reload = false;
const root = computed(() => `/sessions/${props.sessionId}/novel`);
const fileUsers = computed(
  () =>
    state.value?.jobs.filter(
      (job) => job.source && job.source === state.value?.source?.id && job.status !== 'destroyed',
    ).length ?? 0,
);
const fileLocked = computed(() => props.locked || !!fileUsers.value || !!busy.value);
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
  if (!guidanceDirty.value) guidance.value = value.web_guidance;
  savedGuidance.value = value.web_guidance;
  state.value = value;
  received.value = now.value = performance.now();
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
    error.value = '参考文件不能超过 64 MiB';
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
  choosingFile.value = false;
}
type ResearchSettings = Partial<Pick<NovelState, 'enabled' | 'web_enabled' | 'web_guidance'>> & {
  unbind?: boolean;
};
async function configure(patch: ResearchSettings) {
  if (!state.value) return;
  await action('source', () =>
    rawApi(root.value, { expected_revision: state.value!.revision, ...patch }, 'PATCH'),
  );
}
function toggle(event: Event, key: 'enabled' | 'web_enabled') {
  if (!state.value) return;
  const input = event.target as HTMLInputElement;
  const enabled = input.checked;
  input.checked = key === 'enabled' ? state.value.authorized : state.value.web_enabled;
  void configure({ [key]: enabled });
}
async function authorizeAdvisor(event: Event) {
  if (!state.value) return;
  const input = event.target as HTMLInputElement;
  const enabled = input.checked;
  input.checked = state.value.advisor_enabled;
  await action('advisor', () =>
    rawApi(`${root.value}/advisor`, { expected_revision: state.value!.revision, enabled }, 'PATCH'),
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
let clock: number | undefined;
watch(
  () => !!state.value?.jobs.some((job) => job.status === 'running'),
  (running) => {
    window.clearInterval(clock);
    if (running) clock = window.setInterval(() => (now.value = performance.now()), 1000);
  },
);
onBeforeUnmount(() => {
  window.clearInterval(clock);
  disposed = true;
  generation++;
});
</script>

<template>
  <section class="novel-panel" aria-label="资料调查">
    <header class="novel-head">
      <h2><Icon name="book" :size="20" />资料调查</h2>
      <button class="quiet" @click="emit('settings')"><Icon name="sliders" :size="16" />模型与额度</button>
      <p v-if="state" class="novel-summary">
        <span>调查{{ state.enabled ? '已开启' : state.authorized ? '暂不可用' : '已关闭' }}</span>
        <span v-if="state.source && !state.source.missing">· 本地文件</span>
        <span v-if="state.web_enabled && state.can_enable">· 网络查询</span>
        <span :class="{ 'novel-live': state.active }">· 调查者 {{ state.active }}/3</span>
        <FieldHelp tooltip="同一个调查者可以查文件和网页。剧情模型接收调查报告，剧情事实以本局为准。" />
      </p>
    </header>
    <p v-if="novelConnectionError" role="status" class="muted">
      {{ novelConnectionError }} <button class="quiet" @click="retryNovelActivity">重新连接</button>
    </p>
    <p v-if="error" role="alert" class="error">
      {{ error }} <button class="quiet" @click="load">刷新重试</button>
    </p>
    <p v-if="!state" role="status" class="state-empty">正在读取资料调查设置…</p>
    <template v-else>
      <section class="novel-research" aria-label="资料调查者设置">
        <header class="novel-master">
          <h3 class="novel-setting-heading">
            <label class="novel-switch-row"
              ><span>启用资料调查者</span>
              <input
                type="checkbox"
                class="switch"
                role="switch"
                :checked="state.authorized"
                :disabled="!!busy || (!state.authorized && !state.can_enable)"
                @change="toggle($event, 'enabled')"
            /></label>
          </h3>
          <p v-if="!state.can_enable" role="status" class="novel-alert">
            {{ state.authorized ? '缺少可用来源。可关闭总开关撤销授权。' : '缺少可用来源。' }}
            请导入参考文件，或填写调查指引并开启联网查询。
          </p>
          <p class="novel-model">
            <span id="novel-model-name" class="novel-model-name">
              调查模型：{{ providerName(state.effective.profile.provider_id) }} /
              {{ state.effective.profile.model || '尚未配置' }}
            </span>
            <button
              id="novel-model-channel"
              type="button"
              aria-labelledby="novel-model-name novel-model-channel"
              @click="emit('settings')"
            >
              {{ channelLabels[state.effective.channel] }} ·
              {{ state.effective.follows_main ? '跟随主模型' : '用户指定'
              }}<Icon name="arrow-right" :size="12" />
            </button>
            <FieldHelp
              :tooltip="
                (state.effective.channel === 'chatgpt'
                  ? 'OpenAI 官方渠道使用云端压缩。'
                  : '普通 API 使用摘要压缩。') +
                (state.effective.follows_main
                  ? '实际创建时跟随当前剧情回合的模型配置，包括本轮临时思考档位；已启动剧情生成的配置不受中途全局修改影响。'
                  : '')
              "
            />
          </p>
          <p class="novel-privacy">剧情模型按需委派，查阅的资料片段会发送给调查模型。</p>
        </header>
        <section class="novel-source" aria-labelledby="novel-file-heading">
          <h4 id="novel-file-heading" class="novel-spine">资料</h4>
          <div class="novel-source-body">
            <div class="novel-source-row">
              <p v-if="state.source" class="novel-file-name">{{ state.source.name }}</p>
              <p v-else class="novel-file-empty">尚未导入参考文件</p>
              <div v-if="state.source" class="novel-file-actions">
                <button
                  class="icon-button"
                  title="查看副本"
                  aria-label="查看副本"
                  :disabled="state.source.missing"
                  @click="preview(state.source)"
                >
                  <Icon name="eye" :size="16" />
                </button>
                <a
                  v-if="!state.source.missing"
                  class="icon-button"
                  title="下载原文件"
                  aria-label="下载原文件"
                  :href="`/api${root}/sources/${state.source.id}/download`"
                  download
                  ><Icon name="download" :size="16"
                /></a>
                <button
                  class="icon-button"
                  title="解绑"
                  aria-label="解绑"
                  :disabled="fileLocked"
                  @click="configure({ unbind: true })"
                >
                  <Icon name="unlink" :size="16" />
                </button>
              </div>
            </div>
            <template v-if="state.source">
              <p class="novel-meta">
                {{ state.source.characters.toLocaleString() }} 字符 · {{ state.source.encoding }} ·
                {{ when(state.source.created) }}
                <FieldHelp
                  :tooltip="`副本位置：${state.source.location}。外部原文件修改不会自动生效，需要替换文件。`"
                />
              </p>
              <p v-if="state.source.missing" role="alert" class="novel-alert">
                参考文件缺失。{{
                  fileUsers ? '请先销毁使用此文件的调查者，再重新导入。' : '可重新导入文件。'
                }}
              </p>
            </template>
            <div v-if="state.source && !choosingFile" class="novel-file-tools">
              <button
                v-if="state.history.length > 1"
                class="quiet"
                :aria-expanded="history"
                @click="history = !history"
              >
                历史版本（{{ state.history.length }}）<Icon
                  :name="history ? 'chevron-up' : 'chevron-down'"
                  :size="14"
                />
              </button>
              <button :disabled="fileLocked" @click="choosingFile = true">
                <Icon name="import" :size="16" />替换文件
              </button>
            </div>
            <div v-else class="novel-import">
              <label
                >编码<select v-model="encoding" :disabled="fileLocked">
                  <option value="utf-8">UTF-8（默认，识别 BOM）</option>
                  <option value="utf-16">UTF-16</option>
                  <option value="gb18030">GB18030</option>
                  <option value="big5">Big5</option>
                </select></label
              >
              <button :disabled="fileLocked" @click="fileInput?.click()">
                <Icon name="import" :size="16" />{{ busy === 'import' ? '正在导入…' : '选择文件' }}
              </button>
              <button
                v-if="state.source"
                class="quiet"
                :disabled="busy === 'import'"
                @click="choosingFile = false"
              >
                取消
              </button>
              <FieldHelp
                tooltip="支持 txt、md、markdown，最大 64 MiB。应用保存固定副本。调查时，查阅的片段将发送给调查模型。"
              />
            </div>
            <div v-if="history || (!state.source && state.history.length)" class="novel-history">
              <button
                v-for="source in state.history"
                :key="source.id"
                class="quiet"
                :disabled="source.missing"
                @click="preview(source)"
              >
                {{ source.name }} · {{ when(source.created) }} ·
                {{ source.current ? '当前版本' : '历史版本，仅供回查' }}{{ source.missing ? ' · 缺失' : '' }}
              </button>
            </div>
            <input ref="fileInput" type="file" accept=".txt,.md,.markdown" hidden @change="importSource" />
            <p v-if="fileUsers" role="status" class="novel-alert">
              {{ fileUsers }} 个调查者使用当前文件。替换或解绑前请先销毁这些调查者。
            </p>
          </div>
        </section>
        <section class="novel-source" aria-labelledby="novel-web-heading">
          <h4 id="novel-web-heading" class="novel-spine"><label for="novel-web-enabled">联网查询</label></h4>
          <div class="novel-source-body">
            <div class="novel-guidance">
              <div class="novel-source-row">
                <label for="novel-web-guidance">调查指引</label>
                <input
                  id="novel-web-enabled"
                  type="checkbox"
                  class="switch"
                  role="switch"
                  :checked="state.web_enabled"
                  :disabled="!!busy"
                  @change="toggle($event, 'web_enabled')"
                />
              </div>
              <textarea
                id="novel-web-guidance"
                v-model="guidance"
                rows="2"
                :disabled="!!busy"
                placeholder="填写主题、作品名、网址或优先查阅的来源"
              />
            </div>
            <div v-if="guidanceDirty" class="novel-guidance-actions">
              <button :disabled="!!busy" @click="configure({ web_guidance: guidance })">保存指引</button>
              <button class="quiet" :disabled="!!busy" @click="guidance = savedGuidance">撤销修改</button>
            </div>
            <p class="novel-privacy">
              指引供新任务参考。没有可读文件时，启用调查者需要填写指引。<br />查询词和目标网址会发送给联网服务。
            </p>
            <p class="novel-services">
              <button type="button" @click="emit('services')">
                联网服务与代理<Icon name="arrow-right" :size="12" />
              </button>
            </p>
          </div>
        </section>
      </section>
      <section class="novel-advisor" aria-label="参谋 agent 查阅设置">
        <h3 class="novel-setting-heading">
          <label class="novel-switch-row">
            <span>允许参谋 agent 查阅参考文件</span>
            <input
              type="checkbox"
              class="switch"
              role="switch"
              :checked="state.advisor_enabled"
              :disabled="!!busy || (!state.advisor_enabled && (!state.source || state.source.missing))"
              @change="authorizeAdvisor"
            />
          </label>
        </h3>
        <p class="novel-model">
          <button type="button" @click="emit('advisor')">
            参谋模型：{{ advisorModel ? providerName(advisorModel.provider_id) + ' / ' : ''
            }}{{ advisorModel?.model || '尚未配置' }}
            {{ advisorModel ? (state.advisor_model_selection ? ' · 本对话指定' : ' · 跟随全局') : ''
            }}<Icon name="arrow-right" :size="12" />
          </button>
        </p>
        <p class="novel-privacy">
          查阅时原文片段会发送给参谋模型。换模型沿用授权，替换或解绑文件会关闭授权。关闭后，旧片段仍可能随咨询历史发送，可在参谋栏清空咨询。
        </p>
      </section>

      <div class="novel-section-head">
        <h3>
          调查者 <span>{{ state.active }}/3</span>
          <FieldHelp
            tooltip="每轮最多启动五次。剧情生成与调查独立运行，可跨轮；剧情模型应在完成必要追问后积极销毁调查者。停止保留进度，销毁释放名额。"
          />
        </h3>
        <button :disabled="!!busy || (!state.active && !locked)" @click="stopAll">
          <Icon name="stop" :size="16" />全部停止
        </button>
      </div>
      <p v-if="!state.jobs.length" class="state-empty">尚无调查。</p>
      <article v-for="job in jobs" :key="job.id" class="novel-job" :class="statusTone[job.status]">
        <div class="novel-job-head">
          <strong>{{ job.ref }}</strong>
          <span class="novel-tag">{{ novelStatus[job.status] }}</span>
          <span class="novel-job-actions">
            <button
              v-if="job.status === 'running'"
              class="icon-button"
              title="停止调查"
              aria-label="停止调查"
              :disabled="!!busy"
              @click="control(job.ref, 'pause')"
            >
              <Icon name="pause" :size="16" />
            </button>
            <button
              v-if="job.status === 'paused' || job.status === 'failed'"
              class="icon-button"
              title="恢复调查"
              aria-label="恢复调查"
              :disabled="
                !!busy ||
                !!job.access_error ||
                job.requests >= job.config.max_requests ||
                job.elapsed >= job.config.timeout_seconds
              "
              @click="control(job.ref, 'resume')"
            >
              <Icon name="play" :size="16" />
            </button>
            <button
              v-if="job.status !== 'destroyed'"
              class="icon-button"
              title="销毁"
              aria-label="销毁"
              :disabled="!!busy"
              @click="control(job.ref, 'destroy')"
            >
              <Icon name="trash" :size="16" />
            </button>
          </span>
        </div>
        <p class="novel-task">{{ job.task }}</p>
        <p class="novel-job-meta">
          H{{ job.round_position }} · {{ when(job.created) }} ·
          {{ job.config.auto_destroy ? '报告送达后自动销毁' : '保留以便追问' }}
        </p>
        <div class="novel-bars">
          <div>
            <small
              >请求<b>{{ job.requests }} / {{ job.config.max_requests }}</b></small
            ><i :style="{ '--fill': percent(job.requests, job.config.max_requests) }"></i>
          </div>
          <div>
            <small
              >时长<b>{{ Math.floor(elapsed(job)) }} / {{ job.config.timeout_seconds }} 秒</b></small
            ><i :style="{ '--fill': percent(elapsed(job), job.config.timeout_seconds) }"></i>
          </div>
        </div>
        <p v-if="job.pending_messages" class="novel-pending">
          {{ job.pending_messages }} 条待送达消息。{{
            state.enabled
              ? '将在下次剧情请求时送达。'
              : state.authorized
                ? '恢复可用来源后可送达。'
                : '开启调查者后可送达。'
          }}
        </p>
        <p v-if="job.error" role="status" class="error">{{ job.error }}</p>
        <p v-if="job.access_error" class="novel-alert">{{ job.access_error }}</p>
        <button
          class="novel-more"
          :aria-expanded="expanded === job.ref"
          @click="expanded === job.ref ? (expanded = '') : details(job.ref)"
        >
          {{ expanded === job.ref ? '收起详情' : '详情与完整过程' }}
        </button>
        <div v-if="expanded === job.ref" class="novel-trace">
          <p class="muted">
            固定模型：{{ providerName(job.config.profile.provider_id) }} / {{ job.config.profile.model }} ·
            {{ channelLabels[job.config.channel]
            }}{{ job.config.follows_main ? '（创建时跟随主模型）' : '（用户指定）' }}
          </p>
          <p v-if="job.usage.tokens !== undefined" class="muted">
            累计实测输入 {{ job.usage.tokens.toLocaleString() }} tokens · 输出
            {{ job.usage.output_tokens?.toLocaleString() ?? '未知' }}
          </p>
          <p v-if="job.delivered_messages" class="muted">
            已送达剧情模型{{ job.delivered_messages }} 条调查消息。
          </p>
          <p v-if="job.background" class="muted">委派时背景：{{ job.background }}</p>
          <p class="muted">任务来源：{{ jobSources(job) }}</p>
          <div v-if="job.web_enabled && job.web_guidance">
            <strong>任务使用的调查指引</strong>
            <pre>{{ job.web_guidance }}</pre>
          </div>
          <div v-for="(report, index) in reports" :key="index">
            <strong>{{
              report.kind === 'report'
                ? '调查报告'
                : report.kind === 'question'
                  ? '向剧情模型提问'
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
            <WebSources v-if="record.message.web_sources?.length" :sources="record.message.web_sources" />
            <pre v-if="record.message.tool_calls">{{
              JSON.stringify(record.message.tool_calls, null, 2)
            }}</pre>
          </div>
          <button v-if="more" @click="details(job.ref, true)">加载后续记录</button>
        </div>
      </article>
      <button v-if="archiveBefore" class="quiet" @click="loadOlder">加载更早的调查记录</button>
    </template>
    <Teleport to="body">
      <div
        v-if="viewing"
        ref="excerptDialog"
        class="modal-backdrop"
        @keydown.esc.prevent.stop="viewing = null"
      >
        <AppModal label="参考文件副本" size="large" class="novel-excerpt-modal" @close="viewing = null">
          <span class="eyebrow">参考文件副本</span>
          <h2>{{ viewing.name }}</h2>
          <p v-if="!excerpt" role="status" class="muted">正在读取…</p>
          <template v-else>
            <p class="muted">第 {{ excerpt.start }}–{{ excerpt.end }} 字符</p>
            <pre class="novel-excerpt-text">{{ excerpt.text }}</pre>
            <button v-if="excerpt.has_more" @click="preview(viewing!, excerpt.end)">下一段</button>
          </template>
        </AppModal>
      </div>
    </Teleport>
  </section>
</template>
