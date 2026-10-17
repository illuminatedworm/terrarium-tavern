<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import { useAdvisor } from '../../composables/useAdvisor';
import { advisorRunning, toolLabels } from '../../lib/advisor';
import { channelProviders } from '../../lib/channels';
import type { SettingsDocument } from '../../lib/settingsDocument';
import { useModelCatalog } from '../../lib/modelCatalog';
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
}>();
const emit = defineEmits<{ close: []; settings: []; preferenceSaved: [] }>();
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
} = useAdvisor(props.sessionId, () => emit('preferenceSaved'));
const draft = ref(''),
  showModel = ref(false),
  showRange = ref(false),
  showPreview = ref(false),
  confirmClear = ref(false);
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
const profile = computed(() => ({
  ...(base.value || props.settings.shared.advisor.model),
  provider_id: providerId.value,
  model: model.value,
}));
const provider = computed(() => providers.value.find((p) => p.id === providerId.value));
const { catalog } = useModelCatalog(
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
      range.value = value.recent_exchanges;
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
async function clearConversation() {
  if (await clear()) {
    draft.value = '';
    confirmClear.value = false;
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
async function selectRange(value: number | null) {
  await options({ recent_exchanges: value });
  if (!error.value) showRange.value = false;
}
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
        <button :disabled="running || loading || !state" @click="confirmClear = !confirmClear">
          清空对话
        </button>
        <button class="advisor-icon-button" aria-label="关闭参谋 agent" @click="emit('close')">
          <AdvisorIcon name="close" />
        </button>
      </div>
    </header>
    <div v-if="confirmClear" class="advisor-clear-confirm" role="group" aria-label="清空参谋 agent 对话">
      <p>清空聊天、摘要和未完成进度，无法撤销。模型设置与已保存的本轮偏好会保留。</p>
      <button :disabled="running" @click="clearConversation">
        {{ clearing ? '正在清空…' : '确认清空' }}
      </button>
      <button :disabled="clearing" @click="confirmClear = false">取消</button>
    </div>
    <div class="advisor-connection">
      <button
        class="advisor-model-trigger"
        :aria-expanded="showModel"
        :disabled="running || pending"
        @click="
          showModel = !showModel;
          showRange = false;
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
        >同渠道提供商<select v-model="providerId" aria-label="参谋 agent 提供商">
          <option value="" disabled>选择提供商</option>
          <option v-for="p in providers" :key="p.id" :value="p.id">{{ p.name }}</option>
        </select></label
      >
      <label
        >模型<ModelSelect
          v-model="model"
          :models="catalog?.models || []"
          :details="catalog?.model_details"
          label="参谋 agent 模型"
          :disabled="!providerId"
      /></label>
      <div class="advisor-control-actions">
        <button @click="options({ model_selection: null })">使用全局配置</button
        ><button class="primary" :disabled="!providerId || !model.trim()" @click="selectModel">应用</button>
      </div>
    </div>
    <div class="advisor-context-bar">
      <button
        :disabled="running || pending"
        :aria-expanded="showRange"
        @click="
          showRange = !showRange;
          showModel = false;
        "
      >
        {{
          state?.effective.recent_exchanges === 0
            ? '不自动带入剧情'
            : `参考最近 ${state?.effective.recent_exchanges ?? 3} 轮剧情`
        }}
      </button>
      <button ref="previewTrigger" @click="showPreview = true">
        查看上下文<AdvisorIcon name="chevron" />
      </button>
    </div>
    <div v-if="showRange" class="advisor-controls">
      <label
        >近期完整交流数<input
          v-model.number="range"
          type="number"
          min="0"
          max="1000"
          aria-label="参谋 agent 近期交流数"
      /></label>
      <p>
        0 表示关闭。当前使用{{ state?.effective.range_source === 'global' ? '全局默认' : '本故事设置' }}。
      </p>
      <div class="advisor-control-actions">
        <button @click="selectRange(null)">使用全局默认</button
        ><button
          class="primary"
          :disabled="!Number.isInteger(range) || range < 0 || range > 1000"
          @click="selectRange(range)"
        >
          应用
        </button>
      </div>
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
          showRange = false;
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
          v-for="question in ['帮我梳理一下现在的局面。', '下一步可以怎么走？']"
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
