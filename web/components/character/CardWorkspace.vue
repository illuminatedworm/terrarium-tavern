<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { compare } from 'fast-json-patch';
import { api } from '../../lib/api';
import {
  cardName,
  editorTimeline,
  type EditorCard,
  type EditorConversation,
  type EditorPersona,
} from '../../lib/cardEditor';
import type { CardReferenceOption } from '../../lib/cardReferences';
import type { ConversationModelSelection, ModelProfile, Provider } from '../../lib/settingsDocument';
import { editorCacheRate, type EditorContextStats } from '../../lib/editorContextStats';
import { useCardReferencePicker } from '../../composables/useCardReferencePicker';
import { mergeEditorEvents, type EditorEvent } from '../../lib/cardAgentActivity';
import { appendSkillRequest, type EditorSkillPreset } from '../../lib/cardEditorSkills';
import { eventCursor } from '../../lib/turnStream';
import StoryProse from '../story/StoryProse.vue';
import JsonDataTree from '../common/JsonDataTree.vue';
import AppModal from '../common/AppModal.vue';
import CharacterEditor from './CharacterEditor.vue';
import CardWorkspaceIcon from './CardWorkspaceIcon.vue';
import CardRunActivity from './CardRunActivity.vue';
import CardMessage from './CardMessage.vue';
import CardDraftPreview from './CardDraftPreview.vue';
import CardSkills from './CardSkills.vue';
import CardReferencePicker from './CardReferencePicker.vue';
import ReferenceTag from './ReferenceTag.vue';
import ConversationModelControls from '../settings/ConversationModelControls.vue';
import { type Channel } from '../../lib/channels';
import ConversationMessageAdapter from '../settings/ConversationMessageAdapter.vue';
import ChatGPTUsage from '../common/ChatGPTUsage.vue';

const props = defineProps<{
  conversationId: string;
  library: any[];
  modelProfile: ModelProfile;
  providers: Provider[];
}>();
const emit = defineEmits<{ changed: []; saved: []; deleted: [id: string] }>();
const workspace = ref<EditorConversation | null>(null);
const personaLibrary = ref<EditorPersona[]>([]);
const personaAttachments = computed(() => workspace.value?.state.persona_attachments || {});
const input = ref(sessionStorage.getItem(`tavern-editor-input-${props.conversationId}`) || '');
const quickEffort = ref(localStorage.getItem(`tavern-editor-effort-${props.conversationId}`) || 'inherit');
const selectedProfile = computed(() => ({
  ...props.modelProfile,
  ...workspace.value?.model_selection,
}));
async function selectModel(selection: ConversationModelSelection) {
  if (busy.value || active.value) return;
  await guard(async () => {
    workspace.value = await api(
      base + '/model',
      { expected_revision: workspace.value!.revision, selection },
      'PUT',
    );
    localStorage.removeItem(`tavern-editor-model-${props.conversationId}`);
    scheduleStats();
  });
}
async function classifyChannel(channel_origin: Channel) {
  await guard(async () => {
    workspace.value = await api(
      base + '/channel-origin',
      { expected_revision: workspace.value!.revision, channel_origin },
      'PUT',
    );
    emit('changed');
    scheduleStats();
  });
}
const contextStats = ref<EditorContextStats | null>(null);
const statsError = ref('');
const cacheRate = computed(() => editorCacheRate(contextStats.value?.usage));
const contextLabel = computed(
  () =>
    `${contextStats.value?.estimated_tokens?.toLocaleString() ?? '—'} / ${(contextStats.value?.context_limit ?? props.modelProfile.context_tokens).toLocaleString()} tokens`,
);
const contextTitle = computed(
  () =>
    statsError.value ||
    `${contextStats.value?.token_counting?.source === 'reference_fallback' ? '参考分词器' : '计数分词器'} ${contextStats.value?.token_counting?.tokenizer || ''}；当前上下文含输入与工具的本地估算，实际用量以服务商为准。`,
);
const error = ref(''),
  notice = ref(''),
  busy = ref(false);
const selected = ref(''),
  tab = ref('preview'),
  inspectorOpen = ref(false),
  pendingView = ref(false);
const modal = ref(''),
  title = ref(''),
  probe = ref<any>(null);
const composer = ref<HTMLTextAreaElement>(),
  composerFrame = ref<HTMLElement>(),
  dialogInput = ref<HTMLInputElement>();
const chat = ref<HTMLElement>(),
  shell = ref<HTMLElement>(),
  modalOverlay = ref<HTMLElement>();
const followingLatest = ref(true);
const narrow = ref(window.innerWidth <= 1100);
const onResize = () => {
  narrow.value = window.innerWidth <= 1100;
};
const activities = ref<Record<string, EditorEvent[]>>({});
let alive = true;
let refreshVersion = 0;
let stream: EventSource | undefined;
let modalTrigger: HTMLElement | null = null;
let statsTimer: ReturnType<typeof setTimeout> | undefined;
let statsVersion = 0;
const base = `/card-editor/conversations/${props.conversationId}`;
const active = computed(() => workspace.value?.runs.find((r) => ['queued', 'running'].includes(r.status)));
const locked = computed(() => busy.value || !!active.value || !!workspace.value?.pending);
const items = computed(() =>
  pendingView.value && workspace.value?.pending
    ? workspace.value.pending.cards
    : workspace.value?.cards || [],
);
const current = computed(() => items.value.find((c) => c.ref === selected.value) || items.value[0]);
const changes = computed(() =>
  current.value ? compare(current.value.base_document, current.value.document) : [],
);
const timeline = computed(() => editorTimeline(workspace.value?.messages || [], workspace.value?.runs || []));
const conversationTitle = computed(
  () =>
    workspace.value?.title ||
    workspace.value?.messages.find((message) => message.role === 'user')?.content.split('\n')[0] ||
    '新的制卡对话',
);
const draftStatus = computed(() =>
  pendingView.value
    ? '本轮未完成版本'
    : current.value?.kind === 'reference'
      ? '引用快照 · 尚未修改'
      : changes.value.length
        ? '独立草稿 · 有未保存修改'
        : current.value?.card
          ? '与已保存版本一致'
          : '独立草稿 · 尚未保存',
);
const referencePicker = reactive(
  useCardReferencePicker({
    input,
    composer,
    frame: composerFrame,
    cards: computed(() => workspace.value?.cards || []),
    library: computed(() => props.library),
    personas: personaLibrary,
    attachedPersonas: personaAttachments,
    disabled: locked,
    choose: chooseReference,
  }),
);
const lastError = computed(() => workspace.value?.runs.at(-1)?.error);
const formCard = computed(
  () =>
    current.value && {
      ...current.value.source,
      id: `${props.conversationId}-${current.value.ref}`,
      name: cardName(current.value),
      document: current.value.document,
      _draftOnly: false,
    },
);

watch(
  input,
  (value) => {
    sessionStorage.setItem(`tavern-editor-input-${props.conversationId}`, value);
  },
  { flush: 'sync' },
);
watch(quickEffort, (value) => localStorage.setItem(`tavern-editor-effort-${props.conversationId}`, value));
watch([input, quickEffort, () => props.modelProfile], () => scheduleStats());
watch(items, (value) => {
  if (!value.some((c) => c.ref === selected.value)) selected.value = value[0]?.ref || '';
});
watch(modal, (value) => {
  const background = shell.value?.closest<HTMLElement>('.shell');
  if (background) background.inert = !!value;
});

async function guard(action: () => Promise<void>) {
  error.value = '';
  busy.value = true;
  try {
    await action();
  } catch (e: any) {
    if (alive) error.value = e.message;
  } finally {
    if (alive) busy.value = false;
  }
}
async function refresh() {
  const version = ++refreshVersion;
  const value = (await api(base)) as EditorConversation;
  if (!alive || version !== refreshVersion) return;
  workspace.value = value;
  if (!value.pending) pendingView.value = false;
  emit('changed');
  scheduleStats();
}
function modelOptions() {
  return {
    reasoning_effort: quickEffort.value === 'inherit' ? null : quickEffort.value,
  };
}
function scheduleStats() {
  const version = ++statsVersion;
  clearTimeout(statsTimer);
  if (!workspace.value?.channel_origin) return;
  statsTimer = setTimeout(async () => {
    try {
      const value = await api(base + '/stats', { text: input.value, ...modelOptions() });
      if (!alive || version !== statsVersion) return;
      contextStats.value = value;
      statsError.value = '';
    } catch (error: any) {
      if (!alive || version !== statsVersion) return;
      contextStats.value = null;
      statsError.value = `上下文计数不可用：${error.message}`;
    }
  }, 250);
}
function scroll(force = false) {
  if (force) followingLatest.value = true;
  if (!followingLatest.value) return;
  void nextTick(() => chat.value?.scrollTo({ top: chat.value.scrollHeight }));
}
function trackScroll() {
  const element = chat.value;
  if (element) followingLatest.value = element.scrollHeight - element.scrollTop - element.clientHeight < 80;
}
function observe(id: string) {
  stream?.close();
  const accept = eventCursor();
  const connection = new EventSource(`/api/card-edit-runs/${id}/events`);
  stream = connection;
  connection.onmessage = (message) => {
    if (!alive || connection !== stream) return;
    const event = JSON.parse(message.data) as EditorEvent;
    if (!accept(event)) return;
    if (event.kind === 'context_stats') {
      ++statsVersion;
      clearTimeout(statsTimer);
      contextStats.value = { ...contextStats.value, ...event.payload } as EditorContextStats;
      statsError.value = '';
    }
    if (event.kind === 'context_warning') notice.value = event.payload.message;
    if (['model_call', 'text', 'tool_started', 'tool_finished'].includes(event.kind))
      activities.value[id] = mergeEditorEvents(activities.value[id] || [], [event]);
    // tool_finished follows persistence of the working draft. Refresh the
    // opt-in unfinished preview; keep the completed draft selected by default.
    if (
      event.kind === 'tool_finished' &&
      event.payload.status === 'completed' &&
      ['patch_draft', 'set_opening', 'create_draft', 'revert_draft', 'configure_card'].includes(
        event.payload.tool,
      )
    ) {
      void refresh().catch((e: Error) => {
        if (alive && connection === stream) error.value = `草稿预览更新失败：${e.message}`;
      });
    }
    scroll();
  };
  connection.addEventListener('end', () => {
    connection.close();
    if (!alive || connection !== stream) return;
    void guard(async () => {
      await refresh();
      scroll();
    });
  });
}
async function send(resume = false) {
  if (
    !workspace.value?.channel_origin ||
    busy.value ||
    active.value ||
    referencePicker.open ||
    (!resume && !input.value.trim())
  )
    return;
  await guard(async () => {
    const run = await api(base + '/runs', {
      text: input.value,
      expected_revision: workspace.value!.revision,
      operation_id: crypto.randomUUID(),
      resume_run_id: resume ? workspace.value!.pending!.run : null,
      ...modelOptions(),
    });
    if (!alive) return;
    input.value = '';
    await refresh();
    observe(run.id);
    scroll(true);
  });
}
async function stop() {
  if (!active.value) return;
  await guard(async () => {
    await api(`/card-edit-runs/${active.value!.id}/cancel`, {});
    await refresh();
  });
}
async function discard() {
  await guard(async () => {
    await api(base + '/progress', { expected_revision: workspace.value!.revision }, 'DELETE');
    if (!alive) return;
    modal.value = '';
    pendingView.value = false;
    await refresh();
  });
}
async function openModal(name: string) {
  referencePicker.close();
  if (!modal.value) modalTrigger = document.activeElement as HTMLElement | null;
  modal.value = name;
  if (name === 'rename') title.value = workspace.value?.title || '';
  await nextTick();
  const target =
    dialogInput.value || modalOverlay.value?.querySelector<HTMLElement>('button:not([disabled])');
  target?.focus({ preventScroll: true });
}
function closeModal() {
  modal.value = '';
  const trigger = modalTrigger;
  modalTrigger = null;
  void nextTick(() => (trigger?.isConnected ? trigger : composer.value)?.focus({ preventScroll: true }));
}
function confirmCardAction() {
  const kind = modal.value;
  closeModal();
  void action(kind);
}
async function chooseReference(option: CardReferenceOption) {
  if (!workspace.value || locked.value) return;
  const original = input.value;
  const range = referencePicker.query;
  let attached = false;
  await guard(async () => {
    if (option.persona) {
      if (!option.personaRef) {
        const value = await api(base + '/persona-references', {
          persona_id: option.persona.id,
          expected_revision: workspace.value!.revision,
          expected_persona_version: option.persona.version,
        });
        if (!alive) return;
        workspace.value = value;
        emit('changed');
      }
      attached = true;
      scheduleStats();
      return;
    }
    let item = option.attached;
    if (!item && option.source) {
      const value = await api(base + '/references', {
        card_id: option.source.id,
        expected_revision: workspace.value!.revision,
        expected_card_revision: option.source.revision,
      });
      if (!alive) return;
      workspace.value = value;
      item = value.cards.find((card: EditorCard) => card.card === option.source!.id);
      emit('changed');
    }
    if (!alive) return;
    if (!item) throw new Error('角色卡引用未完成，请重试。');
    selected.value = item.ref;
    attached = true;
    scheduleStats();
  });
  if (alive && attached) await referencePicker.complete(original, range);
  else if (alive) {
    await nextTick();
    composer.value?.focus({ preventScroll: true });
  }
}
function inspectReference(item: EditorCard) {
  referencePicker.close();
  selected.value = item.ref;
  inspectorOpen.value = true;
}
async function removeReference(kind: 'card' | 'persona', ref: string) {
  if (locked.value) return;
  await guard(async () => {
    const value = await api(
      base + `/${kind === 'persona' ? 'persona-references' : 'references'}/${ref}`,
      {
        expected_revision: workspace.value!.revision,
      },
      'DELETE',
    );
    if (!alive) return;
    workspace.value = value;
    emit('changed');
    scheduleStats();
  });
}
function composerKeydown(event: KeyboardEvent) {
  if (referencePicker.keydown(event)) return;
  if (event.key === 'Enter' && !event.shiftKey && !event.ctrlKey && !event.altKey && !event.metaKey) {
    event.preventDefault();
    void send(!!workspace.value?.pending);
  }
}
async function referenceFromSkills() {
  closeModal();
  await nextTick();
  await referencePicker.show();
}
function useSkill(preset: EditorSkillPreset) {
  if (!current.value || busy.value || active.value) return;
  input.value = appendSkillRequest(input.value, cardName(current.value), preset);
  closeModal();
  void nextTick(() => composer.value?.focus({ preventScroll: true }));
}
async function action(kind: string) {
  if (!workspace.value || !current.value || locked.value) return;
  const ref = current.value.ref;
  await guard(async () => {
    const revision = { expected_revision: workspace.value!.revision };
    if (kind === 'remove') await api(base + `/references/${ref}`, revision, 'DELETE');
    else if (['undo', 'reload'].includes(kind)) await api(base + `/drafts/${ref}/${kind}`, revision);
    else {
      await api(base + `/drafts/${ref}/save`, { ...revision, mode: kind, operation_id: crypto.randomUUID() });
      if (alive) {
        notice.value = '角色卡已保存。';
        emit('saved');
      }
    }
    await refresh();
  });
}
async function saveForm(document: any) {
  const ref = current.value!.ref;
  const draftKey = `tavern-card-draft-${formCard.value!.id}`;
  await guard(async () => {
    await api(base + `/drafts/${ref}`, { document, expected_revision: workspace.value!.revision }, 'PUT');
    if (!alive) return;
    sessionStorage.removeItem(draftKey);
    closeModal();
    await refresh();
  });
}
async function setTarget(value: string) {
  if (!current.value) return;
  await guard(async () => {
    await api(
      base + `/drafts/${current.value!.ref}`,
      {
        document: current.value!.document,
        target_mode: value || null,
        expected_revision: workspace.value!.revision,
      },
      'PUT',
    );
    await refresh();
  });
}
async function testCard() {
  await guard(async () => {
    const value = await api(base + `/drafts/${current.value!.ref}/test`, {
      expected_revision: workspace.value!.revision,
      index: 0,
    });
    if (alive) {
      probe.value = value;
      tab.value = 'validation';
    }
  });
}
async function rename() {
  await guard(async () => {
    await api(base, { title: title.value.trim(), expected_revision: workspace.value!.revision }, 'PUT');
    if (alive) closeModal();
    await refresh();
  });
}
async function deleteConversation() {
  await guard(async () => {
    await api(base, { expected_revision: workspace.value!.revision }, 'DELETE');
    sessionStorage.removeItem(`tavern-editor-input-${props.conversationId}`);
    if (alive) {
      closeModal();
      emit('deleted', props.conversationId);
    }
  });
}
onMounted(() => {
  window.addEventListener('resize', onResize);
  void guard(async () => {
    await refresh();
    if (!alive) return;
    if (active.value) observe(active.value.id);
    scroll(true);
    const personas = await api('/personas');
    if (!alive) return;
    personaLibrary.value = personas;
  });
});
onBeforeUnmount(() => {
  alive = false;
  ++statsVersion;
  clearTimeout(statsTimer);
  stream?.close();
  window.removeEventListener('resize', onResize);
  const background = shell.value?.closest<HTMLElement>('.shell');
  if (background) background.inert = false;
});
</script>

<template>
  <section ref="shell" class="card-workspace" aria-label="角色卡 Agent" :inert="!!modal">
    <div class="cw-body">
      <div class="cw-dialogue" :inert="inspectorOpen && narrow">
        <header class="cw-header">
          <div class="cw-heading">
            <h1>角色卡编辑</h1>
            <p :title="conversationTitle">{{ conversationTitle }}</p>
          </div>
          <div class="cw-toolbar">
            <button class="cw-quiet" :disabled="locked" @click="openModal('rename')">
              <CardWorkspaceIcon name="pen" />改名
            </button>
            <button class="cw-quiet cw-delete" :disabled="locked" @click="openModal('delete')">
              <CardWorkspaceIcon name="trash" />删除对话
            </button>
            <button
              class="cw-inspector-toggle"
              :aria-expanded="inspectorOpen"
              @click="inspectorOpen = !inspectorOpen"
            >
              卡片与修改
            </button>
          </div>
        </header>
        <p v-if="error" class="cw-notice danger" role="alert">{{ error }}</p>
        <p v-if="notice" class="cw-notice" role="status">{{ notice }}</p>
        <div class="cw-chat-wrap">
          <div ref="chat" class="cw-chat" aria-label="制卡对话" @scroll="trackScroll">
            <div class="cw-chat-content">
              <div v-if="workspace && !workspace.messages.length" class="cw-empty">
                <span class="cw-agent-mark"><CardWorkspaceIcon name="pen" /></span>
                <h2>从一个想法开始</h2>
                <p>讨论人物、世界或互动情境，也可以引用角色卡和用户角色作为资料。</p>
                <p>制作新卡时，先讨论方案，确认后生成可审阅的草稿。</p>
                <p>适配导入卡或迁移状态栏，可使用输入区的“内置技能”。</p>
              </div>
              <template v-for="entry in timeline" :key="entry.key">
                <CardRunActivity
                  v-if="entry.kind === 'activity'"
                  :run="entry.run"
                  :events="activities[entry.run.id]"
                  @content-change="entry.run.id === active?.id && scroll()"
                />
                <CardMessage
                  v-else
                  :role="entry.message.role"
                  :text="entry.message.content"
                  :data-message-ref="entry.message.ref"
                  :data-message-run="entry.message.run"
                />
              </template>
            </div>
          </div>
          <button v-if="!followingLatest" class="cw-latest" aria-label="跳到最新" @click="scroll(true)">
            <CardWorkspaceIcon name="down" />
          </button>
        </div>
        <div v-if="workspace && !workspace.channel_origin" class="cw-pending" role="status">
          <strong>请确认此旧制卡对话的渠道归属，确认后不能更换渠道。</strong>
          <div class="cw-toolbar">
            <button :disabled="busy" @click="classifyChannel('standard')">普通 API</button>
            <button :disabled="busy" @click="classifyChannel('chatgpt')">ChatGPT 官方</button>
          </div>
        </div>
        <div v-if="workspace?.pending && !active" class="cw-pending" role="status">
          <strong>本轮尚未完成，进度已保留</strong>
          <p v-if="lastError">{{ lastError }}</p>
          <div class="cw-toolbar">
            <button
              :disabled="busy"
              @click="
                pendingView = true;
                inspectorOpen = true;
              "
            >
              查看未完成修改
            </button>
            <button :disabled="busy" @click="openModal('discard')">丢弃本轮</button>
          </div>
        </div>
        <form class="cw-composer" @submit.prevent="send(!!workspace?.pending)">
          <div ref="composerFrame" class="cw-composer-frame conversation-compose-frame">
            <CardReferencePicker
              v-if="referencePicker.open"
              :id="referencePicker.id"
              :options="referencePicker.options"
              :active-index="referencePicker.activeIndex"
              :max-height="referencePicker.maxHeight"
              :busy="busy"
              @highlight="referencePicker.activeIndex = $event"
              @choose="chooseReference"
            />
            <div class="cw-references" role="group" aria-label="本对话引用">
              <button
                class="cw-reference-add cw-quiet"
                type="button"
                :disabled="locked"
                :aria-expanded="referencePicker.open"
                :aria-controls="referencePicker.open ? referencePicker.id : undefined"
                @click="referencePicker.show()"
              >
                <CardWorkspaceIcon name="plus" />引用角色
              </button>
              <ReferenceTag
                v-for="item in workspace?.cards"
                :key="item.ref"
                :name="cardName(item)"
                kind="card"
                :disabled="locked"
                @inspect="inspectReference(item)"
                @remove="removeReference('card', item.ref)"
              />
              <ReferenceTag
                v-for="(persona, ref) in personaAttachments"
                :key="ref"
                :name="persona.name"
                kind="persona"
                :disabled="locked"
                @remove="removeReference('persona', ref)"
              />
            </div>
            <textarea
              ref="composer"
              v-model="input"
              aria-label="制卡要求"
              aria-autocomplete="list"
              :aria-controls="referencePicker.open ? referencePicker.id : undefined"
              :aria-activedescendant="referencePicker.open ? referencePicker.activeId : undefined"
              :disabled="busy || !!active"
              :placeholder="
                workspace?.pending
                  ? '可补充新要求后继续，例如：保留 A 的改动，恢复 B…'
                  : '描述想法，或输入 @ 引用角色卡、用户角色…'
              "
              @input="referencePicker.update()"
              @click="referencePicker.update()"
              @keyup="referencePicker.keyup"
              @compositionend="referencePicker.update()"
              @keydown="composerKeydown"
              @blur="!busy && referencePicker.close()"
            />
            <div class="cw-composer-actions">
              <div class="cw-composer-tools">
                <button class="cw-quiet cw-skills-button" type="button" @click="openModal('skills')">
                  <CardWorkspaceIcon name="book" />内置技能
                </button>
                <button class="cw-quiet" type="button" @click="openModal('adapter')">
                  <CardWorkspaceIcon name="message" />消息适配
                </button>
              </div>
              <ConversationModelControls
                v-if="workspace?.channel_origin"
                v-model:effort="quickEffort"
                :profile="selectedProfile"
                :providers="providers"
                :channel="workspace.channel_origin"
                @select="selectModel"
                :disabled="busy || !!active"
              />
              <button v-if="active" class="cw-stop" type="button" :disabled="busy" @click="stop">
                <span aria-hidden="true" />停止
              </button>
              <button
                v-else
                class="primary cw-send conversation-compose-send"
                :disabled="
                  busy ||
                  !workspace?.channel_origin ||
                  referencePicker.open ||
                  (!input.trim() && !workspace?.pending)
                "
              >
                {{ workspace?.pending ? (input.trim() ? '补充要求并继续' : '继续本轮') : '发送' }}
                <CardWorkspaceIcon name="up" />
              </button>
            </div>
          </div>
          <p class="composer-meta cw-composer-meta" aria-label="制卡上下文用量">
            <ChatGPTUsage :active="workspace?.channel_origin === 'chatgpt'" :error="error" />
            <span :title="contextTitle" :class="{ 'cw-stats-error': statsError }">{{ contextLabel }}</span>
            <span title="最近一次模型响应的缓存用量；未提供时不推测命中率">缓存命中率 {{ cacheRate }}</span>
          </p>
        </form>
      </div>
      <aside class="cw-inspector" :class="{ open: inspectorOpen }" aria-label="卡片与修改">
        <button class="cw-close-inspector" @click="inspectorOpen = false">返回对话</button>
        <div class="cw-inspector-top">
          <h2>当前草稿</h2>
          <label
            ><span class="sr-only">当前卡片</span
            ><select v-model="selected" aria-label="选择卡片" :disabled="!items.length">
              <option v-if="!items.length" value="">暂无卡片</option>
              <option v-for="item in items" :key="item.ref" :value="item.ref">
                {{ cardName(item) }}
              </option>
            </select></label
          >
          <label v-if="workspace?.pending" class="check"
            ><input v-model="pendingView" type="checkbox" />查看本轮未完成版本</label
          >
          <p v-if="pendingView" class="cw-notice">未完成版本仅供查看，继续完成后才成为草稿。</p>
        </div>
        <template v-if="current">
          <nav class="cw-tabs" aria-label="卡片信息">
            <button
              v-for="t in [
                { id: 'preview', name: '预览' },
                { id: 'document', name: '数据' },
                { id: 'changes', name: '修改' },
                { id: 'validation', name: '诊断' },
              ]"
              :key="t.id"
              :aria-pressed="tab === t.id"
              @click="tab = t.id"
            >
              {{ t.name }}<span v-if="t.id === 'changes'" class="cw-tab-count">{{ changes.length }}</span>
            </button>
          </nav>
          <div class="cw-inspector-scroll">
            <CardDraftPreview
              v-if="tab === 'preview'"
              :key="current.ref"
              :card="current"
              :status="draftStatus"
              :disabled="locked || pendingView"
              @edit="openModal('form')"
            />
            <template v-else-if="tab === 'document'">
              <p class="muted cw-card-kind">
                {{ current.kind === 'reference' ? '参考卡 · 修改时自动建立草稿' : '独立草稿' }}
              </p>
              <label
                >兼容性目标（可选）<select
                  :value="current.target_mode || ''"
                  :disabled="locked || pendingView"
                  @change="setTarget(($event.target as HTMLSelectElement).value)"
                >
                  <option value="">未限定 · 通用制卡</option>
                  <option value="half_agent">Half Agent</option>
                  <option value="full_agent">Full Agent</option>
                </select></label
              >
              <button class="cw-edit-form" :disabled="locked || pendingView" @click="openModal('form')">
                <CardWorkspaceIcon name="file" />打开表单编辑
              </button>
              <details v-if="current.brief">
                <summary>已确认方案与要求</summary>
                <StoryProse :text="current.brief" />
              </details>
              <JsonDataTree :value="current.document" />
            </template>
            <template v-else-if="tab === 'changes'">
              <p v-if="current.source_changed" class="cw-notice">
                书架原卡已有新版本。覆盖会进行版本检查，当前草稿保持不变。
              </p>
              <div class="cw-save-actions">
                <button :disabled="locked || !current.can_undo || pendingView" @click="action('undo')">
                  撤销上次修改
                </button>
                <button
                  :disabled="locked || !current.source_exists || pendingView"
                  @click="openModal('reload')"
                >
                  重新载入原卡
                </button>
                <button :disabled="locked || pendingView" @click="openModal('remove')">移除引用/草稿</button>
              </div>
              <p v-if="!changes.length" class="muted">与保存基准相同。</p>
              <details v-for="(change, index) in changes" :key="index">
                <summary>{{ change.op }} {{ change.path }}</summary>
                <pre>{{ JSON.stringify(change, null, 2) }}</pre>
              </details>
            </template>
            <template v-else>
              <p>
                {{
                  !current.validation
                    ? '这是本轮未完成版本，检查结果以完成后为准。'
                    : current.validation.valid
                      ? '结构检查通过'
                      : '结构需要修正'
                }}
              </p>
              <p
                v-for="problem in [
                  ...(current.validation?.errors || []),
                  ...(current.validation?.warnings || []),
                ]"
                :key="problem"
              >
                {{ problem }}
              </p>
              <button :disabled="locked || pendingView" @click="testCard">本地验证默认开场</button>
              <p class="muted">检查初始化、脚本启动和正则。实际互动与视觉效果需另行验收。</p>
              <JsonDataTree v-if="probe?.card_ref === current.ref" :value="probe" />
            </template>
          </div>
          <footer class="cw-inspector-footer">
            <span>{{
              pendingView
                ? '未完成修改仅供查看'
                : current.card && !current.source_exists
                  ? '原卡已删除，请另存副本'
                  : draftStatus
            }}</span>
            <div class="cw-save-buttons">
              <button :disabled="locked || pendingView" @click="action('copy')">另存副本</button>
              <button
                class="primary"
                :disabled="locked || pendingView || (!!current.card && !current.source_exists)"
                :title="current.card ? '将当前草稿保存到所引用的书架角色卡' : '将草稿加入书架'"
                @click="action(current.card ? 'overwrite' : 'create')"
              >
                {{ current.card ? '保存角色卡' : '保存新卡' }}
              </button>
            </div>
          </footer>
        </template>
        <p v-else class="cw-empty">
          {{
            Object.keys(personaAttachments).length
              ? '已引用用户角色，可以直接开始讨论。'
              : '还没有角色卡草稿。可以引用角色或直接开始讨论。'
          }}
        </p>
      </aside>
    </div>
  </section>
  <Teleport to="body"
    ><div v-if="modal" ref="modalOverlay" class="modal-backdrop cw-overlay" @keydown.esc="closeModal">
      <AppModal
        :class="{ 'cw-skills-modal': modal === 'skills' }"
        :label="modal === 'form' ? '编辑角色卡草稿' : modal === 'skills' ? '内置技能' : '制卡对话操作'"
        :size="['form', 'adapter'].includes(modal) ? 'large' : modal === 'skills' ? 'medium' : 'small'"
        @close="closeModal"
      >
        <CardSkills
          v-if="modal === 'skills'"
          :cards="items"
          :selected="current?.ref || ''"
          :disabled="busy || !!active"
          @select="selected = $event"
          @use="useSkill"
          @reference="referenceFromSkills"
        />
        <CharacterEditor
          v-else-if="modal === 'form' && formCard"
          :key="formCard.id"
          :card="formCard"
          draft-mode
          @draft-saved="saveForm"
          @back="closeModal"
        />
        <ConversationMessageAdapter v-else-if="modal === 'adapter'" :endpoint="base + '/message-adapter'" />
        <template v-else-if="modal === 'rename'"
          ><h2>对话名称</h2>
          <input ref="dialogInput" v-model="title" maxlength="100" @keydown.enter="rename" /><button
            :disabled="busy || !title.trim()"
            @click="rename"
          >
            保存名称
          </button></template
        >
        <template v-else
          ><h2>
            {{
              modal === 'delete'
                ? '删除制卡对话？'
                : modal === 'discard'
                  ? '丢弃本轮未完成修改？'
                  : modal === 'reload'
                    ? '重新载入原卡？'
                    : '移除这张卡？'
            }}
          </h2>
          <p>
            {{
              modal === 'delete'
                ? '聊天、草稿和撤销记录将删除。已保存的角色卡、用户角色和故事保留。'
                : modal === 'discard'
                  ? '本轮未提交的修改将丢弃，恢复上次完成的草稿。聊天记录保留。'
                  : modal === 'reload'
                    ? '使用书架最新版本替换当前草稿；可通过撤销恢复。'
                    : '这张卡的引用、草稿与撤销记录将移除。书架卡片保留。'
            }}
          </p>
          <div class="cw-toolbar">
            <button @click="closeModal">取消</button
            ><button
              class="danger"
              :disabled="busy"
              @click="
                modal === 'delete'
                  ? deleteConversation()
                  : modal === 'discard'
                    ? discard()
                    : confirmCardAction()
              "
            >
              确认
            </button>
          </div>
        </template>
        <p v-if="error" class="danger" role="alert">{{ error }}</p>
      </AppModal>
    </div></Teleport
  >
</template>

<style scoped src="./CardWorkspace.css"></style>
