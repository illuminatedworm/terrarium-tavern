<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue';
import { useAdvisor } from '../../composables/useAdvisor';
import { advisorOffers, advisorRunning, toolLabels } from '../../lib/advisor';
import { channelProviders } from '../../lib/channels';
import type { SettingsDocument } from '../../lib/settingsDocument';
import { useModelCatalog } from '../../lib/modelCatalog';
import {
  conversationModelProfile,
  modelConnectionSummary,
  selectProfileProvider,
} from '../../lib/modelConnections';
import { positionPopover } from '../../lib/anchoredPopover';
import ModelSelect from '../settings/ModelSelect.vue';
import StoryProse from './AdvisorProse.vue';
import AdvisorIcon from './AdvisorIcon.vue';
import AdvisorPreview from './AdvisorPreview.vue';
import WebSources from '../common/WebSources.vue';
import { webActivityLabel, webSources } from '../../lib/webSearch';
import { handleComposerKeydown, sendShortcutHint } from '../../lib/composerShortcut';

const props = defineProps<{
  sessionId: string;
  storyRevision: number;
  settings: SettingsDocument;
  /** Round of each story message, for the context preview. */
  storyRounds?: number[];
  storyTitle?: string;
}>();
const emit = defineEmits<{
  settings: [];
  preferenceSaved: [];
  started: [];
  useInput: [text: string];
  title: [text: string];
}>();
const {
  state,
  error,
  loading,
  running,
  pending,
  streaming,
  events,
  notice,
  initialize,
  load,
  send,
  options,
  cancel,
  discard,
  clear,
  clearing,
} = useAdvisor(
  props.sessionId,
  () => emit('preferenceSaved'),
  () => emit('started'),
);
const draft = ref(''),
  showModel = ref(false),
  showContextOptions = ref(false),
  showPreview = ref(false),
  confirmClear = ref(false);
const clearTrigger = ref<HTMLButtonElement>(),
  clearPopover = ref<HTMLElement>(),
  clearCancel = ref<HTMLButtonElement>();
const clearPopoverId = useId();
const messages = ref<HTMLElement>(),
  input = ref<HTMLTextAreaElement>();
const previewTrigger = ref<HTMLButtonElement>();
async function closePreview() {
  showPreview.value = false;
  await nextTick();
  previewTrigger.value?.focus();
}
const range = ref(3),
  providerId = ref(''),
  model = ref('');
const channel = computed(() => state.value?.effective.channel || 'standard');
const providers = computed(() => channelProviders(props.settings.shared.providers, channel.value));
const base = computed(() =>
  channel.value === 'chatgpt'
    ? props.settings.shared.advisor.official_model
    : props.settings.shared.advisor.model,
);
const savedProfile = computed(() =>
  conversationModelProfile(
    base.value || props.settings.shared.advisor.model,
    state.value?.effective.model_selection,
  ),
);
const profile = computed(() => ({
  ...selectProfileProvider(savedProfile.value, providerId.value, channel.value === 'chatgpt'),
  model: model.value,
}));
function chooseProvider(value: string) {
  const selected = selectProfileProvider(savedProfile.value, value, channel.value === 'chatgpt');
  providerId.value = selected.provider_id;
  model.value = selected.model;
}
const provider = computed(() => providers.value.find((p) => p.id === providerId.value));
const {
  catalog,
  error: catalogError,
  refresh: refreshModels,
} = useModelCatalog(
  () => profile.value,
  () => provider.value,
  undefined,
  () => showModel.value && !!provider.value,
);
const unavailable = computed(
  () =>
    !state.value?.effective.model_selection?.model || !state.value?.effective.model_selection?.provider_id,
);
const tools = computed(() => events.value.filter((e) => ['tool_started', 'tool_finished'].includes(e.kind)));
const webActivities = computed(() => events.value.filter((e) => e.kind === 'web_activity'));
const liveSources = computed(() =>
  webSources(
    events.value.filter((e) => e.kind === 'web_sources').map((e) => ({ web_sources: e.payload.sources })),
  ),
);
const lastStats = computed(
  () => [...events.value].reverse().find((e) => e.kind === 'context_stats')?.payload,
);
const activeQuestion = computed(() =>
  state.value?.run && (advisorRunning(state.value.run) || pending.value) && !state.value.run.request.compact
    ? state.value.run.request.text
    : '',
);
watch(
  () => state.value?.effective,
  (value) => {
    if (value) {
      // A stepped value waiting to save stays on screen until it is applied.
      if (rangeTimer === undefined && !rangeSaving.value) range.value = value.recent_exchanges;
      providerId.value = value.model_selection?.provider_id || '';
      model.value = value.model_selection?.model || '';
    }
  },
);
watch(
  () => props.storyRevision,
  () => {
    if (!running.value)
      void load(false).catch((e) => {
        error.value = String(e);
      });
  },
);
watch(
  () => props.settings.shared.advisor,
  () => {
    if (!running.value)
      void load(false).catch((e) => {
        error.value = String(e);
      });
  },
  { deep: true },
);
watch(
  () => [streaming.value, state.value?.exchanges.length],
  async () => {
    const follow =
      !messages.value ||
      messages.value.scrollHeight - messages.value.scrollTop - messages.value.clientHeight < 140;
    await nextTick();
    if (follow && messages.value) messages.value.scrollTop = messages.value.scrollHeight;
  },
);
onMounted(initialize);
async function submit() {
  if (!draft.value.trim() || pending.value) return;
  const text = draft.value;
  if (await send(text)) draft.value = '';
}
const clearDisabled = computed(() => running.value || loading.value || !state.value);
async function toggleClear() {
  if (confirmClear.value) return closeClear(true);
  if (clearDisabled.value || !clearPopover.value) return;
  clearPopover.value.showPopover();
  confirmClear.value = true;
  await nextTick();
  positionClear();
  clearCancel.value?.focus({ preventScroll: true });
}
function positionClear() {
  if (clearPopover.value?.matches(':popover-open') && clearTrigger.value)
    positionPopover(clearPopover.value, clearTrigger.value);
}
function closeClear(restoreFocus = false) {
  clearPopover.value?.hidePopover();
  confirmClear.value = false;
  if (restoreFocus) clearTrigger.value?.focus({ preventScroll: true });
}
function syncClear() {
  confirmClear.value = clearPopover.value?.matches(':popover-open') || false;
}
watch(clearDisabled, (disabled) => {
  if (disabled && !clearing.value) closeClear();
});
window.addEventListener('resize', positionClear);
window.addEventListener('scroll', positionClear, true);
async function clearConversation() {
  if (await clear()) {
    draft.value = '';
    closeClear();
    showPreview.value = false;
    await nextTick();
    input.value?.focus();
  }
}
function keydown(event: KeyboardEvent) {
  if (handleComposerKeydown(event, props.settings.shared.send_shortcut)) {
    void submit();
  }
}
async function selectModel() {
  if (!providerId.value || !model.value.trim()) return;
  await options({ model_selection: { provider_id: providerId.value, model: model.value.trim() } });
  if (!error.value) showModel.value = false;
}
// Stepping saves after a short pause so several clicks become one revision-checked
// update; options() would otherwise race overlapping writes on the same revision.
let rangeTimer: number | undefined;
const rangeSaving = ref(false);
const rangeLocked = computed(() => running.value || pending.value || !state.value);
function stepRange(value: number) {
  if (!Number.isFinite(value)) return;
  range.value = Math.min(1000, Math.max(0, Math.round(value)));
  clearTimeout(rangeTimer);
  rangeTimer = window.setTimeout(commitRange, 450);
}
async function commitRange() {
  clearTimeout(rangeTimer);
  rangeTimer = undefined;
  if (rangeSaving.value || !state.value) return;
  const target = range.value;
  if (target === state.value.effective.recent_exchanges) return;
  rangeSaving.value = true;
  try {
    await options({ recent_exchanges: target });
  } finally {
    rangeSaving.value = false;
  }
  if (error.value) range.value = state.value?.effective.recent_exchanges ?? target;
  else if (rangeTimer === undefined && range.value !== target) void commitRange();
}
async function useGlobalRange() {
  clearTimeout(rangeTimer);
  rangeTimer = undefined;
  rangeSaving.value = true;
  try {
    await options({ recent_exchanges: null });
  } finally {
    rangeSaving.value = false;
  }
  if (state.value) range.value = state.value.effective.recent_exchanges;
}
onBeforeUnmount(() => {
  window.removeEventListener('resize', positionClear);
  window.removeEventListener('scroll', positionClear, true);
  if (rangeTimer !== undefined) void commitRange();
});
const offers = computed(
  () => new Map((state.value?.exchanges || []).map((e) => [e.seq, advisorOffers(e.trace)])),
);
// Starter questions double as a list of what the advisor can do.
const starters = computed(() => [
  '帮我梳理一下现在的局面。',
  '下一步可以怎么走？给几个我能直接点选的行动。',
  '查一下人物设定和世界书里关于……的内容。',
  '现在的变量、记忆和剧情规划里记着哪些要点？',
  '我们之前讨论过……，帮我找回当时的结论。',
  ...(props.settings.shared.advisor.web.mode === 'off' ? [] : ['联网查查原著里这段剧情是怎么发展的。']),
  '把“节奏放慢一点，多写对话”加入本轮偏好。',
  '给这个对话起几个标题让我选。',
]);
const callCount = (trace: Array<{ tool_calls?: unknown[] }>) =>
  trace.reduce((sum, m) => sum + (m.tool_calls?.length || 0), 0);
</script>

<template>
  <section class="advisor-panel" aria-label="参谋 agent 聊天">
    <header class="advisor-heading">
      <h2>
        <span class="advisor-mark" aria-hidden="true"><AdvisorIcon name="compass" /></span>参谋
      </h2>
      <div class="advisor-heading-actions">
        <button
          ref="clearTrigger"
          :disabled="clearDisabled"
          aria-haspopup="dialog"
          :aria-expanded="confirmClear"
          :aria-controls="clearPopoverId"
          @click="toggleClear"
        >
          清空对话
        </button>
      </div>
    </header>
    <div
      :id="clearPopoverId"
      ref="clearPopover"
      popover="auto"
      class="advisor-clear-confirm"
      role="dialog"
      aria-label="清空参谋 agent 对话"
      @toggle="syncClear"
      @keydown.esc.prevent.stop="closeClear(true)"
      @click.stop
    >
      <strong>清空参谋 agent 对话？</strong>
      <p>清空聊天、摘要和未完成进度，无法撤销。模型设置与已保存的本轮偏好会保留。</p>
      <div class="advisor-clear-actions">
        <button ref="clearCancel" :disabled="clearing" @click="closeClear(true)">取消</button>
        <button class="danger" :disabled="running || clearing" @click="clearConversation">
          {{ clearing ? '正在清空…' : '确认清空' }}
        </button>
      </div>
    </div>
    <div class="advisor-connection">
      <button
        class="advisor-model-trigger"
        :aria-expanded="showModel"
        :disabled="running || pending"
        @click="
          showModel = !showModel;
          showContextOptions = false;
        "
      >
        <span>{{ state?.effective.model_selection?.model || '独立模型' }}</span
        ><AdvisorIcon name="chevron" :class="{ rotated: showModel }" />
      </button>
      <button
        class="advisor-settings-button"
        aria-label="参谋 agent 设置"
        title="参谋 agent 设置"
        @click="emit('settings')"
      >
        <AdvisorIcon name="settings" />
      </button>
    </div>
    <div v-if="showModel" class="advisor-controls">
      <label
        >同渠道提供商<select
          :value="providerId"
          aria-label="参谋 agent 提供商"
          @change="chooseProvider(($event.target as HTMLSelectElement).value)"
        >
          <option value="" disabled>选择提供商</option>
          <option v-for="p in providers" :key="p.id" :value="p.id">{{ p.name }}</option>
        </select></label
      >
      <label
        >模型<ModelSelect
          v-model="model"
          :models="catalog?.models || []"
          :details="catalog?.model_details"
          :error="catalogError.message"
          :refresh="refreshModels"
          :connection="modelConnectionSummary(profile, provider)"
          label="参谋 agent 模型"
          :disabled="!providerId"
      /></label>
      <div class="advisor-control-actions">
        <button @click="options({ model_selection: null })">使用全局配置</button
        ><button class="primary" :disabled="!providerId || !model.trim()" @click="selectModel">应用</button>
      </div>
    </div>
    <div class="advisor-context-bar">
      <div
        class="advisor-range"
        role="group"
        aria-label="参谋 agent 参考的近期剧情轮数"
        title="0 表示不自动带入剧情"
      >
        <span>{{ range === 0 ? '不带入剧情' : '参考最近' }}</span>
        <span class="advisor-range-stepper"
          ><button
            class="advisor-range-step"
            aria-label="减少一轮"
            :disabled="rangeLocked || range <= 0"
            @click="stepRange(range - 1)"
          >
            −</button
          ><input
            class="advisor-range-value"
            type="number"
            min="0"
            max="1000"
            :value="range"
            :disabled="rangeLocked"
            aria-label="参谋 agent 近期交流数"
            @change="stepRange(($event.target as HTMLInputElement).valueAsNumber)"
            @keydown.enter="commitRange"
          /><button
            class="advisor-range-step"
            aria-label="增加一轮"
            :disabled="rangeLocked || range >= 1000"
            @click="stepRange(range + 1)"
          >
            +
          </button></span
        >
        <span>轮</span>
        <button
          v-if="state?.effective.range_source !== 'global'"
          class="advisor-range-source"
          :disabled="rangeLocked || rangeSaving"
          title="当前为本故事设置，点击清除并使用全局默认"
          @click="useGlobalRange"
        >
          本故事 · 用全局默认
        </button>
        <small v-else class="advisor-range-source" title="在设置页修改全局默认">全局默认</small>
      </div>
      <div class="advisor-context-actions">
        <button
          :disabled="running || pending"
          :aria-expanded="showContextOptions"
          @click="
            showContextOptions = !showContextOptions;
            showModel = false;
          "
        >
          整理<AdvisorIcon name="chevron" :class="{ rotated: showContextOptions }" />
        </button>
        <button ref="previewTrigger" @click="showPreview = true">
          查看上下文<AdvisorIcon name="chevron" />
        </button>
      </div>
    </div>
    <div v-if="showContextOptions" class="advisor-controls">
      <label v-if="channel === 'chatgpt'"
        >咨询上下文方式<select
          :value="state?.effective.context_strategy"
          @change="options({ context_strategy: ($event.target as HTMLSelectElement).value })"
        >
          <option value="local">本地咨询摘要</option>
          <option value="openai_compact">云端 compact</option>
        </select></label
      >
      <button
        :disabled="!state?.exchanges.length || running || pending"
        @click="
          send('', false, true);
          showContextOptions = false;
        "
      >
        立即整理旧咨询
      </button>
    </div>
    <div ref="messages" class="advisor-messages" tabindex="0" aria-label="参谋 agent 聊天记录">
      <p v-if="loading" class="advisor-status" role="status">正在读取咨询…</p>
      <div v-else-if="!state?.exchanges.length && !activeQuestion" class="advisor-empty">
        <AdvisorIcon name="compass" />
        <h3>故事之外，也可以聊聊。</h3>
        <p>回顾线索、查阅资料，或一起想想下一步。</p>
        <button
          v-for="question in starters"
          :key="question"
          @click="
            draft = question;
            input?.focus();
          "
        >
          {{ question }}<AdvisorIcon name="chevron" />
        </button>
      </div>
      <template v-for="exchange in state?.exchanges" :key="exchange.seq">
        <div class="advisor-user-message">{{ exchange.user_text }}</div>
        <article class="advisor-answer">
          <AdvisorIcon name="compass" class="advisor-avatar" />
          <div class="advisor-answer-content">
            <span class="advisor-speaker">参谋 agent</span>
            <div class="advisor-answer-bubble"><StoryProse :text="exchange.response" /></div>
            <div
              v-if="offers.get(exchange.seq)?.inputs.length"
              class="advisor-offers"
              role="group"
              aria-label="可选行动"
            >
              <span>点击填入主对话输入框</span>
              <button
                v-for="(text, i) in offers.get(exchange.seq)!.inputs"
                :key="i"
                @click="emit('useInput', text)"
              >
                {{ text }}
              </button>
            </div>
            <div
              v-if="offers.get(exchange.seq)?.titles.length"
              class="advisor-offers"
              role="group"
              aria-label="候选标题"
            >
              <span>点击设为对话标题</span>
              <button
                v-for="(text, i) in offers.get(exchange.seq)!.titles"
                :key="i"
                :disabled="text === storyTitle"
                @click="emit('title', text)"
              >
                {{ text }}<small v-if="text === storyTitle">当前标题</small>
              </button>
            </div>
            <WebSources :sources="webSources(exchange.trace)" />
            <details v-if="exchange.trace.some((m) => m.web_activity?.length)" class="advisor-tool-record">
              <summary>联网服务记录</summary>
              <p v-for="(item, i) in exchange.trace.flatMap((m) => m.web_activity || [])" :key="i">
                {{ webActivityLabel(item) }}
              </p>
            </details>
            <p v-if="exchange.stale" class="advisor-stale">此讨论依据的剧情已变更</p>
            <details v-if="callCount(exchange.trace)" class="advisor-tool-record">
              <summary>
                <AdvisorIcon name="document" />已查阅与处理 {{ callCount(exchange.trace) }} 项<AdvisorIcon
                  name="chevron"
                />
              </summary>
              <div v-for="(message, i) in exchange.trace" :key="i">
                <template v-if="message.tool_calls"
                  ><p v-for="call in message.tool_calls" :key="call.function.name">
                    {{ toolLabels[call.function.name] || call.function.name }}
                  </p></template
                >
                <pre v-if="message.role === 'tool'">{{ message.content }}</pre>
              </div>
            </details>
          </div>
        </article>
      </template>
      <template v-if="activeQuestion"
        ><div class="advisor-user-message">{{ activeQuestion }}</div>
        <article class="advisor-answer">
          <AdvisorIcon name="compass" class="advisor-avatar" />
          <div class="advisor-answer-content">
            <span class="advisor-speaker">参谋 agent</span>
            <div class="advisor-answer-bubble">
              <StoryProse v-if="streaming" :text="streaming" />
              <p v-else class="advisor-thinking" role="status">
                {{ running ? '正在思考…' : '本次咨询尚未完成' }}
              </p>
            </div>
            <WebSources :sources="liveSources" />
          </div></article
      ></template>
      <details v-if="tools.length && (running || pending)" class="advisor-live-tools">
        <summary>
          正在查阅与处理 · {{ tools.filter((e) => e.kind === 'tool_finished').length }} 项完成
        </summary>
        <p v-for="event in tools" :key="event.seq">
          {{ toolLabels[event.payload.tool] || event.payload.tool }} ·
          {{ event.kind === 'tool_started' ? '开始' : event.payload.result?.error ? '失败' : '完成' }}
        </p>
      </details>
      <details v-if="webActivities.length && (running || pending)" class="advisor-live-tools" open>
        <summary>联网服务</summary>
        <p v-for="event in webActivities" :key="event.seq">{{ webActivityLabel(event.payload) }}</p>
      </details>
    </div>
    <div class="advisor-compose-area">
      <p v-if="notice" class="advisor-notice" role="status">{{ notice }}</p>
      <div v-if="error || state?.run?.error" class="advisor-error" role="alert">
        {{ error || state?.run?.error }}
        <div v-if="pending">
          <button @click="send('', true)">继续咨询</button><button @click="discard">丢弃未完成进度</button>
        </div>
      </div>
      <p v-if="unavailable" class="advisor-setup">
        先为参谋 agent 选择独立模型。<button @click="emit('settings')">前往设置</button>
      </p>
      <div class="advisor-composer">
        <textarea
          ref="input"
          v-model="draft"
          rows="2"
          aria-label="咨询参谋 agent"
          placeholder="聊聊剧情、查资料，或讨论下一步…"
          :disabled="running || pending"
          @keydown="keydown"
        /><button
          v-if="running"
          class="advisor-send"
          aria-label="停止参谋 agent"
          title="停止"
          :disabled="clearing"
          @click="cancel().catch((e) => (error = String(e)))"
        >
          <AdvisorIcon name="stop" /></button
        ><button
          v-else
          class="advisor-send"
          aria-label="发送咨询"
          :disabled="!draft.trim() || pending || unavailable || loading"
          @click="submit"
        >
          <AdvisorIcon name="send" />
        </button>
      </div>
      <div class="advisor-compose-hint">
        <span>{{ sendShortcutHint(settings.shared.send_shortcut) }}</span
        ><span title="只有明确要求保存的本轮偏好会影响正文">讨论不会推进故事</span>
      </div>
      <span v-if="lastStats" class="advisor-token-note"
        >{{ lastStats.estimated_tokens.toLocaleString() }} 估算 tokens{{
          lastStats.opaque ? '（不含云端密文）' : ''
        }}{{
          lastStats.token_counting?.source === 'reference_fallback'
            ? ' · 参考分词器 ' + lastStats.token_counting.tokenizer
            : ''
        }}</span
      >
    </div>
    <AdvisorPreview
      v-if="showPreview"
      :session-id="sessionId"
      :story-rounds="storyRounds"
      @close="closePreview"
    />
  </section>
</template>

<style src="./AdvisorPanel.css"></style>
