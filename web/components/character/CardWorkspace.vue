<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { api } from '../../lib/api';
import {
  cardName,
  draftReview,
  editorTimeline,
  isWorldbook,
  type EditorCard,
  type EditorConversation,
  type EditorPersona,
} from '../../lib/cardAuthor';
import type { CardReferenceOption } from '../../lib/cardReferences';
import type { ConversationModelSelection, ModelProfile, Provider } from '../../lib/settingsDocument';
import { conversationModelProfile } from '../../lib/modelConnections';
import { type ConversationCacheUsage, type EditorContextStats } from '../../lib/editorContextStats';
import { contextTokens, contextUsageDetails, contextUsageLabel } from '../../lib/contextUsage';
import { useCardReferencePicker } from '../../composables/useCardReferencePicker';
import { mergeEditorEvents, type EditorEvent } from '../../lib/cardAgentActivity';
import { appendSkillRequest, type EditorSkillPreset } from '../../lib/cardAuthorSkills';
import { eventCursor } from '../../lib/turnStream';
import StoryProse from '../story/StoryProse.vue';
import JsonDataTree from '../common/JsonDataTree.vue';
import AppModal from '../common/AppModal.vue';
import CharacterEditor from './CharacterEditor.vue';
import WorldbookDraftForm from './WorldbookDraftForm.vue';
import CardWorkspaceIcon from './CardWorkspaceIcon.vue';
import CardRunActivity from './CardRunActivity.vue';
import CardMessage from './CardMessage.vue';
import CardDraftPreview from './CardDraftPreview.vue';
import CardSkills from './CardSkills.vue';
import CardFileReference from './CardFileReference.vue';
import CardReferencePicker from './CardReferencePicker.vue';
import ReferenceTag from './ReferenceTag.vue';
import ConversationModelControls from '../settings/ConversationModelControls.vue';
import { type Channel } from '../../lib/channels';
import ConversationMessageAdapter from '../settings/ConversationMessageAdapter.vue';
import ChatGPTUsage from '../common/ChatGPTUsage.vue';
import CacheRateStat from '../common/CacheRateStat.vue';
import { handleComposerKeydown, sendShortcutHint, type SendShortcut } from '../../lib/composerShortcut';

const props = defineProps<{
  conversationId: string;
  library: any[];
  worldbooks: any[];
  modelProfile: ModelProfile;
  providers: Provider[];
  sendShortcut?: SendShortcut;
}>();
const emit = defineEmits<{ changed: []; saved: []; deleted: [id: string] }>();
const workspace = ref<EditorConversation | null>(null);
const personaLibrary = ref<EditorPersona[]>([]);
const personaAttachments = computed(() => workspace.value?.state.persona_attachments || {});
const pendingFiles = computed(() => (workspace.value?.files || []).filter((file) => !file.message_ref));
const portraitFiles = computed(() =>
  (workspace.value?.files || []).filter((file) =>
    ['.png', '.jpg', '.jpeg', '.webp'].includes(file.extension),
  ),
);
const input = ref(sessionStorage.getItem(`tavern-editor-input-${props.conversationId}`) || '');
const quickEffort = ref(localStorage.getItem(`tavern-editor-effort-${props.conversationId}`) || 'inherit');
const selectedProfile = computed(() =>
  conversationModelProfile(props.modelProfile, workspace.value?.model_selection),
);
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
const cacheUsage = ref<ConversationCacheUsage | null>(null);
const statsError = ref('');
const contextLabel = computed(
  () =>
    `${contextTokens(contextStats.value)?.toLocaleString() ?? '—'} / ${(contextStats.value?.context_limit ?? props.modelProfile.context_tokens).toLocaleString()} tokens · ${contextUsageLabel(contextStats.value)}${busy.value ? ' · 进行中，显示此前快照' : ''}`,
);
const contextTitle = computed(
  () =>
    statsError.value ||
    `${contextUsageDetails(contextStats.value)}${contextStats.value?.image_count ? `当前包含 ${contextStats.value.image_count} 张图片。` : ''}`,
);
const error = ref(''),
  notice = ref(''),
  busy = ref(false);
const uploadingFiles = ref(false);
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
const base = `/card-author/conversations/${props.conversationId}`;
const active = computed(() => workspace.value?.runs.find((r) => ['queued', 'running'].includes(r.status)));
const locked = computed(() => busy.value || !!active.value || !!workspace.value?.pending);
const items = computed(() =>
  pendingView.value && workspace.value?.pending
    ? workspace.value.pending.cards
    : workspace.value?.cards || [],
);
const current = computed(() => items.value.find((c) => c.ref === selected.value) || items.value[0]);
const review = computed(() => draftReview(current.value, pendingView.value));
const changes = computed(() => review.value.changes);
const changeCount = computed(() => review.value.count);
const timeline = computed(() => editorTimeline(workspace.value?.messages || [], workspace.value?.runs || []));
const conversationTitle = computed(
  () =>
    workspace.value?.title ||
    workspace.value?.messages.find((message) => message.role === 'user')?.content.split('\n')[0] ||
    '新的制卡对话',
);
const draftStatus = computed(() => review.value.status);
const worldbook = computed(() => !!current.value && isWorldbook(current.value));
const saveTarget = computed(() => !!(current.value?.card || current.value?.worldbook));
// Built-in skills adapt cards only; a selected worldbook falls back to the first card.
const skillCards = computed(() => items.value.filter((card) => !isWorldbook(card)));
const skillTarget = computed(() => (worldbook.value ? skillCards.value[0] : current.value));
const referencePicker = reactive(
  useCardReferencePicker({
    input,
    composer,
    frame: composerFrame,
    cards: computed(() => workspace.value?.cards || []),
    library: computed(() => props.library),
    worldbooks: computed(() => props.worldbooks),
    personas: personaLibrary,
    attachedPersonas: personaAttachments,
    disabled: locked,
    choose: chooseReference,
  }),
);
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
watch([busy, active], ([busy, active]) => {
  if (!busy && !active) scheduleStats();
});
watch(items, (value) => {
  if (!value.some((c) => c.ref === selected.value)) selected.value = value[0]?.ref || '';
});
// The unfinished-version view should show what the round has changed: when the
// selected card has no pending edit, follow the first card that does.
function pendingChanged(card: EditorCard) {
  const committed = workspace.value?.cards.find((c) => c.ref === card.ref);
  const portrait = (item?: EditorCard) => (item?.source.container === 'png' ? item.source.original : null);
  return (
    JSON.stringify(card.document) !== JSON.stringify(committed?.document) ||
    portrait(card) !== portrait(committed)
  );
}
watch([pendingView, () => workspace.value?.pending?.cards], () => {
  const pending = pendingView.value ? workspace.value?.pending?.cards : undefined;
  if (!pending) return;
  const current = pending.find((c) => c.ref === selected.value);
  const changed = pending.find(pendingChanged);
  if (changed && (!current || !pendingChanged(current))) selected.value = changed.ref;
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
    const running = busy.value || !!active.value;
    try {
      const value = await api(base + '/stats', { text: input.value, ...modelOptions() });
      if (!alive || version !== statsVersion) return;
      contextStats.value = value;
      if (!running && !busy.value && !active.value) cacheUsage.value = value.cache_usage ?? null;
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
    if (
      [
        'model_call',
        'text',
        'tool_started',
        'tool_finished',
        'web_activity',
        'web_sources',
        'response_restarted',
      ].includes(event.kind)
    )
      activities.value[id] = mergeEditorEvents(activities.value[id] || [], [event]);
    // tool_finished follows persistence of the working draft. Refresh the
    // opt-in unfinished preview; keep the completed draft selected by default.
    if (
      event.kind === 'tool_finished' &&
      event.payload.status === 'completed' &&
      [
        'patch_draft',
        'set_text_field',
        'set_opening',
        'set_portrait',
        'create_draft',
        'revert_draft',
        'configure_card',
      ].includes(event.payload.tool)
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
    !selectedProfile.value.model ||
    busy.value ||
    active.value ||
    referencePicker.open ||
    (!resume && !input.value.trim())
  )
    return;
  ++statsVersion;
  clearTimeout(statsTimer);
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
    for (const file of workspace.value!.files || []) {
      if (run.request._file_refs?.includes(file.ref)) file.message_ref = run.request._input_ref;
    }
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
async function addFileReferences(files: File[]) {
  if (locked.value || !workspace.value) return;
  uploadingFiles.value = true;
  try {
    await guard(async () => {
      for (const file of files) {
        if (file.size > 32 * 1024 * 1024) throw new Error(`${file.name} 超过 32 MiB，请先拆分。`);
        const data = new FormData();
        data.append('file', file);
        data.append('expected_revision', String(workspace.value!.revision));
        try {
          const value = await api(base + '/files', data);
          if (!alive) return;
          workspace.value = value;
          emit('changed');
          scheduleStats();
        } catch (error: any) {
          throw new Error(`添加 ${file.name} 失败：${error.message}`);
        }
      }
      composer.value?.focus();
    });
  } finally {
    uploadingFiles.value = false;
  }
}
async function removeFileReference(ref: string) {
  if (locked.value) return;
  await guard(async () => {
    const value = await api(
      base + `/files/${ref}`,
      { expected_revision: workspace.value!.revision },
      'DELETE',
    );
    if (!alive) return;
    workspace.value = value;
    emit('changed');
    scheduleStats();
  });
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
    if (!item && option.worldbookSource) {
      const value = await api(base + '/worldbook-references', {
        worldbook_id: option.worldbookSource.id,
        expected_revision: workspace.value!.revision,
        expected_worldbook_revision: option.worldbookSource.revision,
      });
      if (!alive) return;
      workspace.value = value;
      item = value.cards.find((card: EditorCard) => card.worldbook === option.worldbookSource!.id);
      emit('changed');
    } else if (!item && option.source) {
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
    if (!item) throw new Error('引用未完成，请重试。');
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
  if (handleComposerKeydown(event, props.sendShortcut)) {
    void send(!!workspace.value?.pending);
  }
}
async function referenceFromSkills() {
  closeModal();
  await nextTick();
  await referencePicker.show();
}
function useSkill(preset: EditorSkillPreset) {
  if (!skillTarget.value || busy.value || active.value) return;
  selected.value = skillTarget.value.ref;
  input.value = appendSkillRequest(input.value, cardName(skillTarget.value), preset);
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
        notice.value = worldbook.value ? '世界书已保存。' : '角色卡已保存。';
        emit('saved');
      }
    }
    await refresh();
  });
}
async function setPortrait(select: HTMLSelectElement | null) {
  if (!workspace.value || !current.value || locked.value) return;
  const fileRef = select ? select.value : null;
  if (select) select.value = '';
  if (select && !fileRef) return;
  const ref = current.value.ref;
  await guard(async () => {
    await api(
      base + `/drafts/${ref}/portrait`,
      { expected_revision: workspace.value!.revision, file_ref: fileRef },
      'PUT',
    );
    await refresh();
  });
}
async function saveForm(document: any) {
  const ref = current.value!.ref;
  // Shared with the form components, which keep unsaved form edits under this key.
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
  <section ref="shell" class="card-workspace" aria-label="制卡 agent" :inert="!!modal">
    <div class="cw-body">
      <div class="cw-dialogue" :inert="inspectorOpen && narrow">
        <header class="cw-header">
          <span class="cw-agent-mark" aria-hidden="true"><CardWorkspaceIcon name="pen" /></span>
          <div class="cw-heading">
            <h1>制卡 agent</h1>
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
                <p>讨论人物、世界或互动情境，引用角色卡、世界书、用户角色或本地文件作为资料。</p>
                <p>开启联网后可搜索、读取网页；本地支持文本、DOCX、XLSX 文件及 PNG、JPEG、静态 WebP 图片。</p>
                <p>先讨论方案，再审阅草稿；适配导入卡、迁移状态栏可用“内置技能”。</p>
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
                  :attachments="entry.message.attachments"
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
              <CardFileReference :disabled="locked" :uploading="uploadingFiles" @choose="addFileReferences" />
              <ReferenceTag
                v-for="file in pendingFiles"
                :key="file.ref"
                :name="file.name"
                kind="file"
                :disabled="locked"
                @remove="removeFileReference(file.ref)"
              />
              <ReferenceTag
                v-for="item in workspace?.cards"
                :key="item.ref"
                :name="cardName(item)"
                :kind="isWorldbook(item) ? 'worldbook' : 'card'"
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
                  : '描述想法，或输入 @ 引用角色卡、世界书、用户角色…'
              "
              @input="referencePicker.update()"
              @click="referencePicker.update()"
              @keyup="referencePicker.keyup"
              @compositionend="referencePicker.update()"
              @keydown="composerKeydown"
              :title="sendShortcutHint(sendShortcut)"
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
              <button
                v-if="workspace?.pending && !active"
                class="cw-quiet cw-discard"
                type="button"
                :disabled="busy"
                title="恢复本轮开始前的草稿，保留聊天记录"
                @click="openModal('discard')"
              >
                丢弃本轮
              </button>
              <button v-if="active" class="cw-stop" type="button" :disabled="busy" @click="stop">
                <span aria-hidden="true" />停止
              </button>
              <button
                v-else
                class="primary cw-send conversation-compose-send"
                :disabled="
                  busy ||
                  !workspace?.channel_origin ||
                  !selectedProfile.model ||
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
            <CacheRateStat :usage="cacheUsage" />
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
              {{ t.name }}<span v-if="t.id === 'changes'" class="cw-tab-count">{{ changeCount }}</span>
            </button>
          </nav>
          <div class="cw-inspector-scroll">
            <CardDraftPreview
              v-if="tab === 'preview'"
              :key="current.ref"
              :card="current"
              :conversation-id="conversationId"
              :status="draftStatus"
              :disabled="locked || pendingView"
              @edit="openModal('form')"
            />
            <template v-else-if="tab === 'document'">
              <p class="muted cw-card-kind">
                {{
                  current.kind === 'reference'
                    ? `${worldbook ? '参考世界书' : '参考卡'} · 修改时自动建立草稿`
                    : '独立草稿'
                }}
              </p>
              <label v-if="!worldbook"
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
              <label v-if="!worldbook"
                >头像<select
                  value=""
                  :disabled="
                    locked || pendingView || current.source.container === 'charx' || !portraitFiles.length
                  "
                  @change="setPortrait($event.target as HTMLSelectElement)"
                >
                  <option value="">
                    {{
                      current.source.container === 'charx'
                        ? 'CHARX 卡暂不支持更换头像'
                        : portraitFiles.length
                          ? '选择图片附件作为头像…'
                          : '先在对话中引用图片'
                    }}
                  </option>
                  <option v-for="file in portraitFiles" :key="file.ref" :value="file.ref">
                    {{ file.name }}
                  </option>
                </select></label
              >
              <button
                v-if="!worldbook && current.source.container === 'png'"
                :disabled="locked || pendingView"
                @click="setPortrait(null)"
              >
                移除头像
              </button>
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
                {{
                  worldbook ? '世界书库中的原书' : '书架原卡'
                }}已有新版本。覆盖会进行版本检查，当前草稿保持不变。
              </p>
              <div class="cw-save-actions">
                <button :disabled="locked || !current.can_undo || pendingView" @click="action('undo')">
                  撤销上次修改
                </button>
                <button
                  :disabled="locked || !current.source_exists || pendingView"
                  @click="openModal('reload')"
                >
                  {{ worldbook ? '重新载入原书' : '重新载入原卡' }}
                </button>
                <button :disabled="locked || pendingView" @click="openModal('remove')">移除引用/草稿</button>
              </div>
              <p v-if="!changeCount" class="muted">与保存基准相同。</p>
              <p v-if="current.portrait_changed" class="cw-notice">
                {{ current.card ? '头像与书架已保存版本不同。' : '新卡头像尚未保存到书架。' }}
              </p>
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
              <template v-if="!worldbook">
                <button :disabled="locked || pendingView" @click="testCard">本地验证默认开场</button>
                <p class="muted">检查初始化、脚本启动和正则。实际互动与视觉效果需另行验收。</p>
              </template>
              <JsonDataTree v-if="probe?.card_ref === current.ref" :value="probe" />
            </template>
          </div>
          <footer class="cw-inspector-footer">
            <span>{{
              pendingView
                ? '未完成修改仅供查看'
                : saveTarget && !current.source_exists
                  ? `原${worldbook ? '世界书' : '卡'}已删除，请另存副本`
                  : draftStatus
            }}</span>
            <div class="cw-save-buttons">
              <button :disabled="locked || pendingView" @click="action('copy')">另存副本</button>
              <button
                class="primary"
                :disabled="locked || pendingView || (saveTarget && !current.source_exists)"
                :title="
                  worldbook
                    ? '将当前草稿保存回所引用的世界书'
                    : current.card
                      ? '将当前草稿保存到所引用的书架角色卡'
                      : '将草稿加入书架'
                "
                @click="action(saveTarget ? 'overwrite' : 'create')"
              >
                {{ worldbook ? '保存世界书' : current.card ? '保存角色卡' : '保存新卡' }}
              </button>
            </div>
          </footer>
        </template>
        <p v-else class="cw-empty">
          {{
            Object.keys(personaAttachments).length
              ? '已引用用户角色，可以直接开始讨论。'
              : '还没有草稿。可以引用角色卡、世界书或直接开始讨论。'
          }}
        </p>
      </aside>
    </div>
  </section>
  <Teleport to="body"
    ><div v-if="modal" ref="modalOverlay" class="modal-backdrop cw-overlay" @keydown.esc="closeModal">
      <AppModal
        :class="{ 'cw-skills-modal': modal === 'skills' }"
        :label="
          modal === 'form'
            ? worldbook
              ? '编辑世界书草稿'
              : '编辑角色卡草稿'
            : modal === 'skills'
              ? '内置技能'
              : '制卡对话操作'
        "
        :size="['form', 'adapter'].includes(modal) ? 'large' : modal === 'skills' ? 'medium' : 'small'"
        @close="closeModal"
      >
        <CardSkills
          v-if="modal === 'skills'"
          :cards="skillCards"
          :selected="skillTarget?.ref || ''"
          :disabled="busy || !!active"
          @select="selected = $event"
          @use="useSkill"
          @reference="referenceFromSkills"
        />
        <WorldbookDraftForm
          v-else-if="modal === 'form' && formCard && worldbook"
          :key="formCard.id"
          :draft-key="`tavern-card-draft-${formCard.id}`"
          :document="current!.document"
          @draft-saved="saveForm"
          @back="closeModal"
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
                    ? worldbook
                      ? '重新载入原世界书？'
                      : '重新载入原卡？'
                    : worldbook
                      ? '移除这本世界书？'
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
                    ? `使用${worldbook ? '世界书库' : '书架'}最新版本替换当前草稿；可通过撤销恢复。`
                    : worldbook
                      ? '这本世界书的引用、草稿与撤销记录将移除。世界书库中的书保留。'
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
