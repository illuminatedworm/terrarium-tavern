<script setup lang="ts">
import { notification, notify } from './lib/notifications';
import { useTimedError } from './composables/useTimedError';
import {
  computed,
  defineAsyncComponent,
  h,
  nextTick,
  onMounted,
  onBeforeUnmount,
  provide,
  ref,
  watch,
} from 'vue';
import { useContextOperations } from './composables/useContextOperations';
import { useModelWarmup, type LocalModelStatus, type StartupModels } from './composables/useModelWarmup';
import { channelLabels, type Channel } from './lib/channels';
import { api } from './lib/api';
import { loreTitle } from './lib/loreBook';
import type { ConversationModelSelection, SettingsDocument, SettingsObject } from './lib/settingsDocument';
import { conversationModelProfile } from './lib/modelConnections';
import { usesChatGPT } from './lib/chatgptAccounts';
import { canRetryTurn } from './lib/turnRetry';
import { effortOptions } from './lib/reasoningEffort';
import ChatGPTUsage from './components/common/ChatGPTUsage.vue';
import { conversationCacheRate, conversationCacheRateTitle } from './lib/editorContextStats';
import { cacheRetention } from './lib/cachePolicy';
import { historyMessageIntervals } from './lib/storyHistory';
import LoreInjectionRecords from './components/records/LoreInjectionRecords.vue';
import { dialogueLoreRecords, type LoreRecord } from './lib/loreRecords';
import ContextPreview from './components/story/ContextPreview.vue';
import FullAgentTools from './components/story/FullAgentTools.vue';
import FullAgentDialogue from './components/story/FullAgentDialogue.vue';
import HalfAgentDialogue from './components/story/HalfAgentDialogue.vue';
import DialogueMessage from './components/story/DialogueMessage.vue';
import InlineMessageEditor from './components/story/InlineMessageEditor.vue';
import CacheBreakpointPanel from './components/story/CacheBreakpointPanel.vue';
import ConversationRail from './components/story/ConversationRail.vue';
import DialogueTools from './components/story/DialogueTools.vue';
import ExpandableTextarea from './components/common/ExpandableTextarea.vue';
import PromptField from './components/common/PromptField.vue';
import { highlightSource } from './lib/sceneNotes';
import StoryProse from './components/story/StoryProse.vue';
import ExperimentalRecords from './components/story/ExperimentalRecords.vue';
import { latestAuxiliary } from './lib/kemini';
import { clampRightWidth, defaultReadingLayout, type ReadingLayout } from './lib/dataTransfer';
import ReadingLayoutPanel from './components/settings/ReadingLayoutPanel.vue';
import ConversationModelControls from './components/settings/ConversationModelControls.vue';
import TurnReview from './components/story/TurnReview.vue';
import RecipeEditor from './components/settings/RecipeEditor.vue';
import ConversationMessageAdapter from './components/settings/ConversationMessageAdapter.vue';
import CharacterPortrait from './components/character/CharacterPortrait.vue';
import Icon from './components/common/Icon.vue';
import { cardPortraitUrl, sessionPortraitUrl } from './lib/portraits';
import StoryWizard from './components/story/StoryWizard.vue';
import MvuInspector from './components/story/MvuInspector.vue';
import SceneInspector from './components/story/SceneInspector.vue';
import SessionOptions, { type SessionOptionsDraft } from './components/story/SessionOptions.vue';
import StoryStatePanel, { type StateTab } from './components/story/StoryStatePanel.vue';
import MemoryRebuildConfirm from './components/story/MemoryRebuildConfirm.vue';
import FullAgentWindowPanel from './components/story/FullAgentWindowPanel.vue';
import ForkSelectPanel from './components/story/ForkSelectPanel.vue';
import ForkConfirmPanel from './components/story/ForkConfirmPanel.vue';
import DeleteCardConfirm from './components/character/DeleteCardConfirm.vue';
import AboutPanel from './components/common/AboutPanel.vue';
import HomeView from './components/story/HomeView.vue';
import SessionList from './components/story/SessionList.vue';
import CardList from './components/character/CardList.vue';
import CardConversationList from './components/character/CardConversationList.vue';
import WorldbookList from './components/character/WorldbookList.vue';
import StoryLoading from './components/story/StoryLoading.vue';
import PersonaEditor from './components/settings/PersonaEditor.vue';
import WorkRecords from './components/records/WorkRecords.vue';
import AppModal from './components/common/AppModal.vue';
import StartupWarmup from './components/story/StartupWarmup.vue';
import { observeTurn, turnDraft, type TraceEvent } from './lib/turnStream';
import { createSessionController, StaleView } from './lib/sessionController';
import { contextKind, groupContext, summarizeBudgetSources } from './lib/contextPreview';
import type {
  BranchMessage,
  CardConversationSummary,
  JsonObject,
  CardRecord,
  MemoryRebuildEstimate,
  OrphanDraft,
  PendingInput,
  Session,
  SessionSummary,
  StoryContextPreview,
  StoryMessageRecord,
  TurnRecord,
  TurnRequest,
  Worldbook,
  WorldbookSummary,
} from './lib/types';

const SettingsPanel = defineAsyncComponent({
  loader: () => import('./components/settings/SettingsPanel.vue'),
  delay: 0,
  loadingComponent: { render: () => h('p', { role: 'status' }, '正在加载设置…') },
  errorComponent: { render: () => h('p', { role: 'alert' }, '设置页面加载失败，请刷新页面后重试。') },
});
const AdvisorPanel = defineAsyncComponent(() => import('./components/story/AdvisorPanel.vue'));
// Editing surfaces are independent of the story reader; load their code only
// when opened, just as we do for settings.
const CharacterEditor = defineAsyncComponent({
  loader: () => import('./components/character/CharacterEditor.vue'),
  delay: 0,
  loadingComponent: { render: () => h('p', { role: 'status' }, '正在加载角色卡编辑器…') },
  errorComponent: { render: () => h('p', { role: 'alert' }, '角色卡编辑器加载失败，请刷新页面后重试。') },
});
const WorldbookEditor = defineAsyncComponent({
  loader: () => import('./components/character/WorldbookEditor.vue'),
  delay: 0,
  loadingComponent: { render: () => h('p', { role: 'status' }, '正在加载世界书编辑器…') },
  errorComponent: { render: () => h('p', { role: 'alert' }, '世界书编辑器加载失败，请刷新页面后重试。') },
});
const CardWorkspace = defineAsyncComponent({
  loader: () => import('./components/character/CardWorkspace.vue'),
  delay: 0,
  loadingComponent: { render: () => h('p', { role: 'status' }, '正在加载制卡界面…') },
  errorComponent: { render: () => h('p', { role: 'alert' }, '制卡界面加载失败，请刷新页面后重试。') },
});

const orphanDrafts = ref<OrphanDraft[]>([]);
const worldbooks = ref<WorldbookSummary[]>([]),
  selectedWorldbook = ref<Worldbook | null>(null);
const cardConversations = ref<CardConversationSummary[]>([]);
const editorConversationId = ref('');
const deletingCardConversationId = ref('');
const cards = ref<CardRecord[]>([]),
  sessions = ref<SessionSummary[]>([]),
  session = ref<Session | null>(null);
const sessionCard = ref<CardRecord | null>(null);
const sessionPortraits = computed(() =>
  Object.fromEntries(sessions.value.map((s) => [s.id, sessionPortraitUrl(s)])),
);
const storyPortrait = computed(() => sessionPortraitUrl(session.value));
const savingSessionTitle = ref(false);
const isFullAgent = computed(() => session.value?.agent_mode === 'full_agent');
/** Turn actions are reachable only while a turn is shown. */
function currentTurn(): TurnRecord {
  if (!activeTurn.value) throw new Error('没有进行中的回合');
  return activeTurn.value;
}
/** Story actions are reachable only from the open story view. */
function openStory(): Session {
  if (!session.value) throw new Error('没有打开的故事');
  return session.value;
}
const advisorSeen = ref('');
function applyFullSession(updated: Session) {
  if (session.value?.id === updated.id && updated.revision >= openStory().revision) {
    session.value = updated;
    void refreshContextStats();
  }
}
const contextOps = useContextOperations(() => session.value, applyFullSession);
const contextBusy = contextOps.busy;
provide('context-operations', contextOps);
const settingsChannel = ref<Channel>();
async function classifyChannel(channel: Channel) {
  const sid = session.value?.id;
  try {
    const updated = await api(
      `/sessions/${sid}/channel-origin`,
      { channel_origin: channel, expected_revision: openStory().revision },
      'PUT',
    );
    applyFullSession(updated);
    sessions.value = await api('/sessions');
  } catch (e: any) {
    notify(e.message);
  }
}
const dialogue = ref<{
  jumpTo: (id: string) => Promise<void> | undefined;
  jumpBoundary: (edge: 'start' | 'end') => Promise<void> | undefined;
  releaseAnchor: () => void;
}>();
const readingMessageId = ref('');
const messageIntervals = computed(() => historyMessageIntervals(session.value?.messages || []));
const eligibleCacheReplies = computed(() => {
  const messages = session.value?.messages || [];
  const roles = new Map(messages.map((message) => [message.id, message.role]));
  return new Set(
    messages
      .filter(
        (message) => message.role === 'assistant' && !!message.parent && roles.get(message.parent) === 'user',
      )
      .map((message) => message.id),
  );
});
const input = ref(''),
  error = ref(''),
  busy = ref(false),
  activeTurn = ref<TurnRecord | null>(null),
  draft = ref('');
const panel = ref(''),
  settings = ref<SettingsDocument | null>(null),
  selected = ref<CardRecord | null>(null);
const preview = ref<StoryContextPreview | null>(null),
  trace = ref<TraceEvent[]>([]),
  savingRecipe = ref(false);
const previewMessagesBusy = ref(false);
const settingsPanel = ref<InstanceType<typeof SettingsPanel>>();
const settingsInitialPage = ref('');
watch(panel, (value) => {
  if (value !== 'settings') {
    settingsInitialPage.value = '';
    settingsChannel.value = undefined;
  }
});
function managePreferencePresets() {
  settingsInitialPage.value = 'shared.preference_presets';
  panel.value = 'settings';
}
/** Views that read settings open only after `load()`. */
function loadedSettings(): SettingsDocument {
  if (!settings.value) throw new Error('设置尚未载入');
  return settings.value;
}
const activeSettings = computed<SettingsObject>(
  () => settings.value?.[isFullAgent.value ? 'full_agent' : 'half_agent'] ?? {},
);
const configuredModel = computed(
  () =>
    (isFullAgent.value
      ? activeSettings.value[session.value?.channel_origin === 'chatgpt' ? 'official_model' : 'model']
      : activeSettings.value[
          session.value?.channel_origin === 'chatgpt' ? 'official_foreground' : 'foreground'
        ]) || {},
);
const activeModel = computed(() =>
  conversationModelProfile(configuredModel.value, session.value?.model_selection),
);
const modelSaving = ref(false);
async function selectStoryModel(selection: ConversationModelSelection) {
  if (!session.value || modelSaving.value) return;
  const sid = openStory().id;
  const token = viewScope.current();
  modelSaving.value = true;
  try {
    const result = await viewApi(
      `/sessions/${sid}/model`,
      { expected_revision: openStory().revision, selection },
      'PUT',
    );
    if (viewScope.owns(token) && session.value?.id === sid) {
      session.value = result;
      await refreshContextStats();
    }
  } catch (e: any) {
    if (viewScope.owns(token)) error.value = e.message;
  } finally {
    modelSaving.value = false;
  }
}
const activeSubscription = computed(() => usesChatGPT(activeModel.value, settings.value?.shared.providers));
const channelModelsReady = computed(
  () =>
    !!activeModel.value.model &&
    (isFullAgent.value ||
      !!activeSettings.value[
        session.value?.channel_origin === 'chatgpt' ? 'official_background' : 'background'
      ]?.model),
);
const trackerSubscription = computed(
  () =>
    !isFullAgent.value &&
    usesChatGPT(
      activeSettings.value[
        session.value?.channel_origin === 'chatgpt' ? 'official_background' : 'background'
      ],
      settings.value?.shared.providers,
    ),
);
const storySubscriptionLabel = computed(() =>
  activeSubscription.value
    ? trackerSubscription.value || isFullAgent.value
      ? '使用 ChatGPT 订阅'
      : '正文使用 ChatGPT 订阅'
    : '后台使用 ChatGPT 订阅',
);
function closePanel() {
  if (panel.value === 'settings' && settingsPanel.value) void settingsPanel.value.requestClose();
  else panel.value = '';
}
const startupModels = ref<StartupModels | null>(null);
const modelWarmup = useModelWarmup(() => panel.value === 'warmup');
const contextStats = ref<StoryContextPreview | null>(null);
const contextEstimateError = ref('');
let contextStatsRequest = 0;
let contextRefreshTimer: ReturnType<typeof setTimeout> | undefined;
watch(input, () => {
  clearTimeout(contextRefreshTimer);
  contextRefreshTimer = setTimeout(() => {
    if (!busy.value) void refreshContextStats();
  }, 350);
});
const workCount = ref(0),
  rewriteExpanded = ref(false);
const rewriteToggle = ref<HTMLButtonElement>();
const updatingVariables = computed(
  () =>
    busy.value &&
    [...trace.value]
      .reverse()
      .find((event) =>
        ['variable_update_started', 'variable_update_completed', 'variable_update_failed'].includes(
          event.kind,
        ),
      )?.kind === 'variable_update_started',
);
const updatingTracker = computed(
  () =>
    busy.value &&
    [...trace.value]
      .reverse()
      .find((event) => ['tracker_started', 'tracker_completed', 'tracker_failed'].includes(event.kind))
      ?.kind === 'tracker_started',
);
const retryNote = ref(''),
  retryIncludeReply = ref(false);
const storyLoad = ref<{ id: string; creating: boolean; name: string; error: string } | null>(null);
watch(
  () => [session.value?.id ?? storyLoad.value?.id, activeTurn.value?.id, activeTurn.value?.status],
  () => {
    retryNote.value = activeTurn.value?.request?.regenerate_note || '';
    retryIncludeReply.value = !!activeTurn.value?.request?.regenerate_include_reply;
    const request = activeTurn.value?.status !== 'committed' ? activeTurn.value?.request : null;
    allowEdit.value = !!request?.allow_content_edit;
  },
);
const viewScope = createSessionController();
const viewApi = viewScope.request;
function abandonStoryLoad() {
  if (!storyLoad.value) return;
  viewScope.enter();
  storyLoad.value = null;
  session.value = null;
  sessionCard.value = null;
  activeTurn.value = null;
  busy.value = false;
}
function retryStoryLoad() {
  const pending = storyLoad.value;
  if (pending) void openSession(pending.id, pending.creating);
}
const memoryWarning = ref('');
watch(
  () => [session.value?.id, session.value?.snapshot?.memory_warning],
  () => {
    memoryWarning.value = session.value?.snapshot?.memory_warning || '';
  },
);
useTimedError(memoryWarning);
const memoryDiscardNotice = ref('');
useTimedError(memoryDiscardNotice, 5000);
watch(
  () => session.value?.id,
  () => {
    memoryDiscardNotice.value = '';
  },
);
const pendingInput = ref<PendingInput | null>(null);
// Finished turns are reopened without replaying SSE, so recover their input
// from the persisted request, including replacements awaiting an atomic commit.
const visiblePendingInput = computed((): PendingInput | null => {
  if (pendingInput.value) return pendingInput.value;
  const request = activeTurn.value?.request;
  if (!request || request.scene_refresh) return null;
  const text = request.mode === 'regenerate' ? request.replacement_text : request.text;
  return text ? { raw: text } : null;
});
const pendingLore = ref<LoreRecord[]>([]);
const pendingVisible = computed(
  () =>
    activeTurn.value &&
    !currentTurn().request.scene_refresh &&
    (['queued', 'running', 'awaiting_review'].includes(currentTurn().status) ||
      canRetryTurn(activeTurn.value, session.value)),
);
watch(
  () => session.value?.id,
  () => {
    pendingInput.value = null;
    pendingLore.value = [];
  },
);
const updateVariables = ref(false);
const variablePreferenceSaving = ref(false);
const allowEdit = ref(false);
watch([updateVariables, () => allowEdit.value], () => {
  if (!busy.value) void refreshContextStats();
});
watch(
  () => [
    session.value?.id,
    session.value?.update_variables,
    session.value?.disabled_tools,
    activeTurn.value?.id,
    activeTurn.value?.status,
  ],
  () => {
    // Running/reviewed turns show their captured permission; future turns use
    // the conversation preference, including after navigation or a reload.
    const turn = activeTurn.value;
    const request =
      turn && ['queued', 'running', 'awaiting_review'].includes(turn.status) ? turn.request : null;
    updateVariables.value =
      !!(request?.update_variables ?? session.value?.update_variables) &&
      !(session.value?.agent_mode === 'full_agent' && openStory().disabled_tools?.includes('patch_state'));
  },
);
async function saveUpdateVariables() {
  const sid = openStory().id,
    token = viewScope.current();
  variablePreferenceSaving.value = true;
  try {
    const updated = await viewApi(
      `/sessions/${sid}/options`,
      { expected_revision: openStory().revision, update_variables: updateVariables.value },
      'PUT',
    );
    // Patch in place; the conversation settings page keeps its own unsaved draft.
    openStory().update_variables = updated.update_variables;
    openStory().revision = updated.revision;
  } catch (e: any) {
    if (viewScope.owns(token)) {
      updateVariables.value = !!openStory().update_variables;
      error.value = e.message;
    }
  } finally {
    variablePreferenceSaving.value = false;
  }
}
const chat = ref<HTMLElement>();
// The glass composer floats over the message list so prose scrolls beneath it;
// its measured height reserves matching space at the end of the list and lifts
// the overlays (rail, jump buttons, regeneration panel) above it.
const composer = ref<HTMLElement>();
const composerHeight = ref(0);
let composerObserver: ResizeObserver | undefined;
watch(composer, (element, previous) => {
  if (previous) composerObserver?.unobserve(previous);
  if (!element) return;
  composerObserver ??= new ResizeObserver(() => {
    if (composer.value) composerHeight.value = composer.value.offsetHeight;
  });
  composerObserver.observe(element);
});
const followLatest = ref(true);
const readingPinned = ref(false);
const pendingReplyStart = ref('');
const pageMode = ref(''),
  regenerateNote = ref(''),
  regenerateIncludeReply = ref(false),
  quickEffort = ref('inherit');
const editingMessageId = ref(''),
  editingMessageText = ref('');
const savingMessageEdit = ref(false);
let editingWasFollowing = false;
const sceneSaving = ref<'memory' | null>(null);
const memoryRebuildEstimate = ref<MemoryRebuildEstimate | null>(null);
const memoryProgress = computed(() => {
  if (sceneUpdating.value !== 'memory') return '';
  const batch = [...trace.value].reverse().find((e) => e.kind === 'memory_batch');
  return batch
    ? `正在整理第 ${batch.payload.primary_calls} 批，已提交的旧记忆保持不变。`
    : '正在准备重建记忆…';
});
const sourceHighlight = ref<{ id: string; excerpt: string; fallback: boolean } | null>(null);
const cacheBreakpointBusy = ref(false);
const writerResponseCache = computed(() => {
  const profile = activeModel.value;
  return !!session.value && profile?.cache?.mode === 'runtime' && writerCacheRetention.value.explicit;
});
const writerCacheRetention = computed(() => {
  const profile = activeModel.value;
  const retention = cacheRetention(
    profile?.model || '',
    profile?.api_mode || 'chat_completions',
    profile?.cache || { mode: 'cloud', preset: 'auto', field: 'cache_control', value: {}, request: {} },
  );
  return activeSubscription.value
    ? {
        ...retention,
        apiLabel: 'ChatGPT 订阅 · Responses',
        explicit: false,
        enabled: false,
        options: [],
        protocolNote: '',
        note: '缓存由服务端管理，此通道不支持显式缓存控制。',
      }
    : retention;
});
const cacheRequestState = computed(
  () =>
    [...trace.value]
      .reverse()
      .find(
        (event) =>
          event.kind === 'provider_request' &&
          event.payload?.cache &&
          ['full_agent', 'narrative'].includes(event.payload.purpose),
      )?.payload.cache,
);
const cacheBreakpointId = computed(() => {
  const planned = contextStats.value?.cache;
  return planned && planned.scope === session.value?.id && planned.revision === session.value?.revision
    ? planned.message_id || null
    : session.value?.cache_breakpoint_effective_id || null;
});
const sentCacheLabel = computed(() => {
  const cache = cacheRequestState.value;
  const at = session.value?.messages.findIndex((m) => m.id === cache?.message_id) ?? -1;
  return at >= 0 ? `历史 H${at + 1}` : contextLabel(cache?.source || '固定前缀');
});
const sceneSubmitting = ref<'memory' | null>(null);
const storyView = ref<'dialogue' | 'work'>('dialogue');
const sceneUpdating = computed(
  () => sceneSubmitting.value || (busy.value ? activeTurn.value?.request?.scene_refresh : null),
);
const sidebarSection = ref(
  ['cards', 'worldbooks', 'editor'].includes(localStorage.getItem('tavern-sidebar-section') || '')
    ? localStorage.getItem('tavern-sidebar-section')!
    : 'sessions',
);
type DeleteTarget =
  | { kind: 'session'; item: SessionSummary }
  | { kind: 'editor'; item: CardConversationSummary }
  | { kind: 'worldbook'; item: WorldbookSummary };
const deleteBubble = ref<
  (DeleteTarget & { title: string; above: boolean; left: number; arrowLeft: number; top: number }) | null
>(null);
const inspectorTab = ref('mvu'),
  optionsNotice = ref('');
const inspectorPages = computed(() =>
  [
    { id: 'advisor', label: '参谋', icon: 'compass' },
    { id: 'cache', label: '缓存', icon: 'bolt' },
    isFullAgent.value ? { id: 'tools', label: '工具', icon: 'wrench' } : null,
    { id: 'mvu', label: 'MVU 变量', icon: 'braces' },
    { id: 'scene', label: '场景', icon: 'map' },
    { id: 'options', label: '会话设置', icon: 'sliders' },
  ].filter((page) => !!page),
);
// Mirrors the left rail: clicking the open page collapses, any other page opens there.
function openInspector(id: string) {
  if (inspectorTab.value === id && !layout.value.rightCollapsed) return void toggleRightSidebar();
  inspectorTab.value = id;
  if (layout.value.rightCollapsed) void toggleRightSidebar();
}
// Once opened, the advisor chat stays mounted for this story so a running reply
// survives switching to another inspector page.
watch([inspectorTab, () => session.value?.id], ([tab, id]) => {
  if (tab === 'advisor' && id) advisorSeen.value = id;
});
let optionsNoticeTimer: ReturnType<typeof setTimeout> | undefined;
function clearOptionsNotice() {
  clearTimeout(optionsNoticeTimer);
  optionsNoticeTimer = undefined;
  optionsNotice.value = '';
}
function showOptionsNotice(message: string) {
  clearOptionsNotice();
  optionsNotice.value = message;
  optionsNoticeTimer = setTimeout(clearOptionsNotice, 2000);
}
const deletingCard = ref<CardRecord | null>(null),
  deletingCardBusy = ref(false);
const forkMessage = ref<BranchMessage | null>(null),
  creatingFork = ref(false);
const forkPoints = ref<BranchMessage[]>([]),
  forkFromPicker = ref(false);
const regeneratingId = computed(() =>
  busy.value && activeTurn.value?.request?.mode === 'regenerate' ? currentTurn().request.message_id : null,
);
let deleteTrigger: HTMLElement | null = null;
watch(sidebarSection, (value) => localStorage.setItem('tavern-sidebar-section', value));
const layout = ref<ReadingLayout>({
  ...defaultReadingLayout(window.innerWidth),
  ...JSON.parse(localStorage.getItem('tavern-layout') || '{}'),
});
watch(layout, (value) => localStorage.setItem('tavern-layout', JSON.stringify(value)), { deep: true });
// On <html> so teleported dialogs and popovers follow the same material choice.
watch(
  () => layout.value.glass !== false,
  (glass) => document.documentElement.classList.toggle('no-glass', !glass),
  { immediate: true },
);
const mobileSidebarQuery = window.matchMedia('(max-width: 620px)');
const mobileSidebar = ref(mobileSidebarQuery.matches);
const rightOverlayQuery = window.matchMedia('(max-width: 1200px)');
const rightOverlay = ref(rightOverlayQuery.matches);
// Drawers are transient on narrow screens: a stored "expanded" state (often saved
// while a desktop window was being narrowed) must not reopen one over the story.
if (rightOverlay.value) layout.value.rightCollapsed = true;
if (mobileSidebar.value) layout.value.leftCollapsed = true;
const rightDrawerOpen = computed(
  () => rightOverlay.value && !!session.value && !pageMode.value && !layout.value.rightCollapsed,
);
watch(
  rightDrawerOpen,
  (open) => {
    if (open) layout.value.leftCollapsed = true;
  },
  { immediate: true },
);
const leftSidebarClose = ref<HTMLButtonElement | null>(null);
const mobileSidebarButton = ref<HTMLButtonElement | null>(null);
const rightSidebarToggle = ref<HTMLButtonElement | null>(null);
const rightSidebarOpen = ref<HTMLButtonElement | null>(null);
function syncMobileSidebar(event: MediaQueryListEvent) {
  mobileSidebar.value = event.matches;
}
function syncRightOverlay(event: MediaQueryListEvent) {
  rightOverlay.value = event.matches;
  // Zooming or narrowing a desktop window must not pop the inspector open over the story.
  if (event.matches) layout.value.rightCollapsed = true;
}
async function toggleLeftSidebar() {
  if (layout.value.leftCollapsed && mobileSidebar.value) layout.value.rightCollapsed = true;
  layout.value.leftCollapsed = !layout.value.leftCollapsed;
  if (!mobileSidebar.value) return;
  await nextTick();
  (layout.value.leftCollapsed ? mobileSidebarButton.value : leftSidebarClose.value)?.focus();
}
async function toggleRightSidebar() {
  if (layout.value.rightCollapsed && rightOverlay.value) layout.value.leftCollapsed = true;
  layout.value.rightCollapsed = !layout.value.rightCollapsed;
  if (!rightOverlay.value) return;
  await nextTick();
  (layout.value.rightCollapsed ? rightSidebarOpen.value : rightSidebarToggle.value)?.focus();
}
function closeRightSidebar() {
  if (!layout.value.rightCollapsed) void toggleRightSidebar();
}
const readingStyle = computed(() => ({
  '--story-font-size': layout.value.fontSize + 'px',
  '--story-line-height': layout.value.lineHeight,
  '--reading-width': layout.value.readingWidth + 'px',
  '--story-font-family':
    layout.value.fontFamily === 'serif'
      ? '"Noto Serif SC", "SimSun", Georgia, serif'
      : '"Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif',
}));
let stopRightResize: undefined | (() => void);
function resizeRightBy(delta: number) {
  layout.value.rightWidth = clampRightWidth(layout.value.rightWidth + delta);
}
function startRightResize(event: PointerEvent) {
  if (layout.value.rightCollapsed || rightOverlay.value) return;
  event.preventDefault();
  stopRightResize?.();
  const startX = event.clientX,
    startWidth = layout.value.rightWidth;
  const move = (e: PointerEvent) => {
    layout.value.rightWidth = clampRightWidth(startWidth + startX - e.clientX);
  };
  const stop = () => {
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', stop);
    document.body.classList.remove('resizing-sidebar');
    stopRightResize = undefined;
  };
  stopRightResize = stop;
  document.body.classList.add('resizing-sidebar');
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', stop);
}
let previousFocus: HTMLElement | null = null;
watch(panel, async (value) => {
  if (value) {
    previousFocus = document.activeElement as HTMLElement;
    await nextTick();
    (
      document.querySelector(
        '.modal input, .modal select, .modal textarea, .modal button',
      ) as HTMLElement | null
    )?.focus();
  } else {
    await nextTick();
    if (previousFocus?.isConnected) previousFocus.focus();
  }
});

// Story-changing controls wait for the running turn, an open message edit and any review.
const storyLocked = computed(
  () => busy.value || !!editingMessageId.value || activeTurn.value?.status === 'awaiting_review',
);
const retryableTurn = computed(() =>
  canRetryTurn(activeTurn.value, session.value) ? activeTurn.value : null,
);
watch(
  () => activeTurn.value?.status,
  (status) => {
    const request = retryableTurn.value?.request;
    // Failed input stays directly editable; an automatic floating panel would
    // cover its retry controls. Plain regeneration keeps its rewrite options.
    rewriteExpanded.value =
      status === 'error' && request?.mode === 'regenerate' && request.replacement_text == null;
  },
);
function turnError(value: string) {
  return value?.includes('runtime.variableUpdate') || value?.includes('MVU_UPDATE_INVALID')
    ? '模型的变量更新无效，本次正文与状态未提交。草稿已保留，可以点击“重新生成”。'
    : value;
}
const latestReply = computed(() =>
  session.value?.messages.at(-1)?.role === 'assistant' && session.value?.messages.at(-1)?.parent
    ? openStory().messages.at(-1)
    : null,
);
const editableMessageIds = computed(() => {
  const request = retryableTurn.value?.request;
  // A failed send/replacement owns the latest input; edit its persisted request
  // as a fresh attempt instead of offering another edit on the committed pair.
  if (request && (request.mode === 'send' || request.replacement_text != null)) return new Set<string>();
  const user = session.value?.messages.at(-2);
  return new Set(user?.role === 'user' && user.id === latestReply.value?.parent ? [user.id] : []);
});
const mvuPanel = computed(() => {
  for (const message of [...(session.value?.messages || [])].reverse()) {
    if (message.presentation?.panel.length)
      return {
        ...message,
        id: openStory().head,
        snapshot: openStory().snapshot,
      };
  }
  return null;
});
const scriptPermissionBusy = ref(false);
const scriptPermissionDisabled = computed(
  () =>
    busy.value ||
    scriptPermissionBusy.value ||
    contextBusy.value ||
    activeTurn.value?.status === 'awaiting_review',
);
async function setSessionScriptPermission(enabled: boolean) {
  const token = viewScope.current();
  const sid = session.value?.id;
  if (!sid || busy.value || scriptPermissionBusy.value) return;
  scriptPermissionBusy.value = true;
  try {
    const result = await viewApi(
      `/sessions/${sid}/script-permission`,
      { enabled, expected_revision: openStory().revision },
      'PUT',
    );
    if (!viewScope.owns(token)) return;
    sessionCard.value = result.card;
    openStory().revision = result.session.revision;
    // Reload resolved display assets without reinitializing variables.
    if (enabled) {
      const updated = await viewApi(`/sessions/${sid}`);
      if (!viewScope.owns(token)) return;
      openStory().messages = updated.messages;
      openStory().snapshot = updated.snapshot;
      openStory().revision = updated.revision;
    }
  } catch (e: any) {
    if (viewScope.owns(token)) notify(e.message);
  } finally {
    scriptPermissionBusy.value = false;
  }
}
let events: EventSource | undefined;
const currentCard = computed(
  () =>
    cards.value.find((c) => c.id === session.value?.card) ||
    (sessionCard.value?.id === session.value?.card ? sessionCard.value : null),
);
const name = computed(() => currentCard.value?.name || '新的故事，正等着你');
const archiveCharacter = computed(
  () => sessionCard.value?.document?.data?.nickname || sessionCard.value?.document?.nickname || name.value,
);
const contextLabels: Record<string, string> = {
  rules: '规则',
  lore: '世界书（按设定位置）',
  lore_constant: '顶部常驻世界书',
  lore_depth: '世界书（历史深度插入）',
  lore_conversation: '中途注入世界书',
  character: '角色',
  card_system_prompt: '角色卡系统提示',
  card_personality: '角色卡性格',
  card_scenario: '角色卡场景',
  system_prompt: '角色卡系统提示',
  description: '角色卡描述',
  personality: '角色卡性格',
  scenario: '角色卡场景',
  persona: '用户',
  examples: '示例',
  memory: '记忆',
  handoff: '交接笔记',
  note_list: '长期笔记目录',
  agent_tools: '当前窗口工具记录',
  context_status: '窗口与记忆状态',
  context_policy: '上下文窗口机制',
  cloud_compaction: '云端压缩状态',
  context_budget: '窗口预算提示',
  mvu_rules: 'MVU 变量规则',
  turn_variable_instruction: '本轮变量授权',
  tool_definitions: '工具定义',
  history: '历史',
  tool_protocol: '工具协议',
  plot: '剧情规划',
  author_note: '作者注释',
  state: '状态',
  input: '本轮输入',
  post: '后置指令',
  inject: '脚本注入',
  turn_instruction: '补充要求',
  regenerate_instruction: '重生成要求',
  regenerate_reference: '待重写回复参考',
  'provider-fallback': '兼容提示',
  session_preference: '本轮偏好',
  chat_framing: '请求封装',
};
function contextLabel(source: string) {
  const kind = contextKind(source);
  return contextLabels[source] || contextLabels[kind] || source || '其他';
}
const loreRecords = computed(() => {
  const messages = session.value?.messages || [];
  return dialogueLoreRecords(
    pendingVisible.value ? [...messages, { snapshot: { lore_injections: pendingLore.value } }] : messages,
  );
});
const pendingInputLore = computed(() => {
  const inputId = pendingInput.value?.id;
  // Regeneration reuses an input whose lore is already shown at its history anchor.
  if (session.value?.messages.some((message) => message.id === inputId)) return [];
  return pendingLore.value.filter((record) => !record.manually_removed && record.anchor_id === inputId);
});
const removingLore = ref(false);
async function removeLore(record: LoreRecord) {
  if (removingLore.value || busy.value || activeTurn.value?.status === 'awaiting_review') return;
  removingLore.value = true;
  try {
    await guarded(async () => {
      session.value = await viewApi(
        `/sessions/${openStory().id}/lore-injections/${encodeURIComponent(record.anchor_id)}/${encodeURIComponent(record.id)}`,
        { expected_revision: openStory().revision },
        'DELETE',
      );
      pendingLore.value = pendingLore.value.filter(
        (item) => item.id !== record.id || item.anchor_id !== record.anchor_id,
      );
      await refreshContextStats();
    });
  } finally {
    removingLore.value = false;
  }
}
const contextGroups = computed(() => groupContext(preview.value?.trace || [], contextLabel));
const budgetSourceSummary = computed(() =>
  summarizeBudgetSources(
    preview.value?.budget_sources,
    Number(preview.value?.estimated_tokens || 0),
    contextLabel,
  ),
);
const stateTab = ref<StateTab>('memory');
// Rounds count user turns; replies belong to the round of the input they answer.
const messageRounds = computed(() => {
  const rounds = new Map<string, number>();
  let round = 0;
  for (const message of session.value?.messages || []) {
    if (message.role === 'user') round += 1;
    rounds.set(message.id, round);
  }
  return rounds;
});
const storyRounds = computed(() =>
  (session.value?.messages || []).map((message) => messageRounds.value.get(message.id)!),
);
function contextItemTitle(item: JsonObject) {
  if (item.title) return item.title;
  const source = String(item.source || '');
  if (source.startsWith('lore:')) {
    const entry = preview.value?.lore?.find((e) => String(e.id) === source.slice(5));
    return loreTitle(entry);
  }
  if (source === 'persona') return `用户 · ${session.value?.persona || ''}`;
  if (source === 'agent_tools') {
    if (item.role === 'tool') {
      const call = (preview.value?.trace || [])
        .flatMap((message) => message.tool_calls || [])
        .find((call: JsonObject) => call.id === item.tool_call_id);
      return `工具返回 · ${call?.function?.name || item.tool_call_id || '工具'}`;
    }
    const names = (item.tool_calls || []).map((call: JsonObject) => call.function?.name || '工具').join('、');
    return names ? `工具调用 · ${names}` : `${name.value} · 中间消息`;
  }
  if (source === 'history') return `${item.role === 'user' ? session.value?.persona : name.value} · 历史消息`;
  return contextLabel(source);
}
function usageValue(usage: any, paths: string[]) {
  for (const path of paths) {
    let value = usage;
    for (const key of path.split('.')) value = value?.[key];
    if (typeof value === 'number') return value;
  }
  return null;
}
const cacheRateLabel = computed(() =>
  conversationCacheRate(contextEstimateError.value ? null : contextStats.value?.cache_usage),
);
const composerContextLabel = computed(() => {
  const current = contextEstimateError.value
    ? '—'
    : (contextStats.value?.estimated_tokens?.toLocaleString() ?? '—');
  return `${current} / ${activeModel.value.context_tokens.toLocaleString()} tokens`;
});
const composerContextTitle = computed(
  () =>
    contextEstimateError.value ||
    `当前上下文 / 上下文窗口上限；${tokenizerLabel.value || '当前上下文为本地估算，实际用量以服务商为准。'}${contextStats.value?.compaction_opaque ? '当前仅估算可见文本，不含云端加密压缩状态。' : ''}`,
);
const contextLengthLabel = computed(() =>
  contextEstimateError.value
    ? '本次 token 计数不可用'
    : contextStats.value?.estimated_tokens == null
      ? '计算中'
      : `${contextStats.value.compaction_opaque ? '可见文本估算' : '本次预计输入'} ${Number(contextStats.value.estimated_tokens).toLocaleString()} tokens${contextStats.value.compaction_opaque ? '（不含加密状态）' : ''}`,
);
const tokenizerLabel = computed(() => {
  if (contextEstimateError.value) return contextEstimateError.value;
  const info = contextStats.value?.token_counting;
  if (!info) return '';
  return `${info.source === 'reference_fallback' ? '参考分词器' : '计数分词器'} ${info.tokenizer}；包含消息封装估算，实际用量以服务商为准。`;
});
const actualInputLabel = computed(() => {
  const value = usageValue(contextStats.value?.usage, ['prompt_tokens', 'input_tokens', 'promptTokenCount']);
  return value == null ? '上次实际输入：暂无数据' : `上次实际输入 ${value.toLocaleString()} tokens`;
});
async function guarded(fn: () => Promise<any>) {
  error.value = '';
  try {
    return await fn();
  } catch (e: any) {
    if (!(e instanceof StaleView)) error.value = e.message;
  }
}
async function load() {
  [
    cards.value,
    sessions.value,
    settings.value,
    orphanDrafts.value,
    cardConversations.value,
    worldbooks.value,
  ] = await Promise.all([
    api('/cards'),
    api('/sessions'),
    api('/settings'),
    api('/card-edits'),
    api('/card-author/conversations'),
    api('/worldbooks'),
  ]);
}
async function afterDataImport(result: {
  settings_restored: boolean;
  browser_preferences: { layout?: Record<string, unknown> };
}) {
  if (result.settings_restored && result.browser_preferences.layout)
    Object.assign(layout.value, result.browser_preferences.layout);
  await guarded(load);
}
async function refreshContextStats() {
  if (!session.value) return;
  const request = ++contextStatsRequest;
  const sid = openStory().id,
    token = viewScope.current();
  try {
    const [stats, records] = await Promise.allSettled([
      api(`/sessions/${sid}/preview`, {
        text: input.value,
        update_variables: updateVariables.value,
        allow_content_edit: allowEdit.value,
      }),
      api(`/sessions/${sid}/work-records?limit=1`),
    ]);
    if (!viewScope.owns(token) || request !== contextStatsRequest) return;
    if (stats.status === 'fulfilled') {
      contextStats.value = stats.value;
      contextEstimateError.value = '';
    } else contextEstimateError.value = String(stats.reason?.message || stats.reason);
    if (records.status === 'fulfilled') workCount.value = records.value.total;
  } catch {
    /* Explicit preview presents errors; a stats refresh must not break reading. */
  }
}
async function openSession(id: string, creating = false, createdSession?: Session) {
  pendingReplyStart.value = '';
  regenerateIncludeReply.value = false;
  rewriteExpanded.value = false;
  const token = viewScope.enter();
  storyLoad.value = {
    id,
    creating,
    name: sessions.value.find((item) => item.id === id)?.title || '',
    error: '',
  };
  busy.value = true;
  error.value = '';
  session.value = null;
  sessionCard.value = null;
  activeTurn.value = null;
  contextStats.value = null;
  contextEstimateError.value = '';
  clearTimeout(contextRefreshTimer);
  workCount.value = 0;
  events?.close();
  events = undefined;
  sidebarSection.value = 'sessions';
  pageMode.value = '';
  inspectorTab.value = 'mvu';
  clearOptionsNotice();
  sourceHighlight.value = null;
  trace.value = [];
  draft.value = '';
  cancelMessageEdit();
  quickEffort.value = localStorage.getItem(`tavern-effort-${id}`) || 'inherit';
  if (window.innerWidth < 760) layout.value.leftCollapsed = true;
  try {
    const [opened, card]: [Session, CardRecord] = await Promise.all([
      createdSession || api(`/sessions/${id}`),
      api(`/sessions/${id}/card`),
    ]);
    if (!viewScope.owns(token)) return;
    const active = opened.turns.find((turn) =>
      ['running', 'queued', 'awaiting_review'].includes(turn.status),
    );
    const last = active || opened.turns[0];
    // An active turn is always reloaded below, so only full turns are restored.
    let restored: TurnRecord | null = null;
    let restoredDraft = '';
    if (
      last &&
      ['running', 'queued', 'awaiting_review', 'error', 'cancelled', 'interrupted'].includes(last.status)
    ) {
      const turn: TurnRecord = await api(`/turns/${last.id}`);
      if (!viewScope.owns(token)) return;
      if (active || canRetryTurn(turn, opened)) {
        restored = turn;
        restoredDraft = turn.draft;
      }
    }
    session.value = opened;
    sessionCard.value = card;
    activeTurn.value = restored;
    draft.value = restoredDraft;
    busy.value = !!active && active.status !== 'awaiting_review';
    if (window.innerWidth < 760) layout.value.leftCollapsed = true;
    localStorage.setItem('tavern-session', id);
    // Publish the complete reading state before dismissing the loading screen.
    storyLoad.value = null;
    if (busy.value && restored) observe(restored.id);
    const latest = opened.messages
      .slice()
      .reverse()
      .find((message) => message.role === 'assistant');
    if (!restored && latest) await navigateMessage(latest.id);
    else await scroll(true);
    if (viewScope.owns(token)) void refreshContextStats();
  } catch (e: any) {
    if (!viewScope.owns(token)) return;
    session.value = null;
    sessionCard.value = null;
    activeTurn.value = null;
    busy.value = false;
    if (storyLoad.value) storyLoad.value.error = e.message || String(e);
  }
}

function askDeleteConversation(target: DeleteTarget, event: MouseEvent) {
  deleteTrigger = event.currentTarget as HTMLElement;
  const rect = deleteTrigger.getBoundingClientRect(),
    width = 270,
    height = 190,
    below = rect.bottom + height + 8 < window.innerHeight,
    left = Math.max(10, Math.min(window.innerWidth - width - 10, rect.right - width));
  deleteBubble.value = {
    ...target,
    title:
      target.kind === 'worldbook'
        ? target.item.name
        : target.item.title || (target.kind === 'session' ? target.item.name : ''),
    above: !below,
    left,
    arrowLeft: Math.max(12, Math.min(width - 12, rect.left + rect.width / 2 - left)),
    top: below ? rect.bottom + 6 : Math.max(10, rect.top - height - 6),
  };
  nextTick(() => document.querySelector<HTMLElement>('.delete-confirm-popover button')?.focus());
}
function closeDeleteBubble(restoreFocus = true) {
  deleteBubble.value = null;
  if (restoreFocus && deleteTrigger?.isConnected) deleteTrigger.focus();
  deleteTrigger = null;
}
function closeDeleteOnOutside(event: PointerEvent) {
  const target = event.target as Element;
  if (deleteBubble.value && !target.closest('.delete-confirm-popover,.session-delete'))
    closeDeleteBubble(false);
}
function closeDeleteOnViewportChange(event?: Event) {
  if (event?.target instanceof Element && event.target.closest('.delete-confirm-popover')) return;
  if (deleteBubble.value) closeDeleteBubble(false);
}
async function deleteCard() {
  if (!deletingCard.value || deletingCardBusy.value) return;
  deletingCardBusy.value = true;
  try {
    await guarded(async () => {
      const id = deletingCard.value!.id;
      await api(`/cards/${id}`, {}, 'DELETE');
      if (session.value?.card === id) sessionCard.value = await api(`/sessions/${openStory().id}/card`);
      if (selected.value?.id === id) {
        selected.value = null;
        if (pageMode.value === 'card') pageMode.value = '';
      }
      await load();
      deletingCard.value = null;
      panel.value = '';
    });
  } finally {
    deletingCardBusy.value = false;
  }
}
async function deleteSession(item: SessionSummary) {
  closeDeleteBubble(false);
  const isCurrent = session.value?.id === item.id || storyLoad.value?.id === item.id;
  if (isCurrent) {
    abandonStoryLoad();
    viewScope.enter();
    events?.close();
  }
  await guarded(async () => {
    await api(`/sessions/${item.id}`, {}, 'DELETE');
    localStorage.removeItem(`tavern-effort-${item.id}`);
    if (localStorage.getItem('tavern-session') === item.id) localStorage.removeItem('tavern-session');
    if (isCurrent) {
      session.value = null;
      activeTurn.value = null;
      draft.value = '';
      trace.value = [];
      contextStats.value = null;
    }
    await load();
  });
}
async function renameFromList(item: SessionSummary, title: string) {
  savingSessionTitle.value = true;
  try {
    return (
      (await guarded(async () => {
        await renameSession(item, title);
        return true;
      })) === true
    );
  } finally {
    savingSessionTitle.value = false;
  }
}
async function renameSession(item: SessionSummary | Session, title: string) {
  const updated = await api(
    `/sessions/${item.id}/title`,
    {
      title,
      expected_revision: session.value?.id === item.id ? openStory().revision : item.revision,
    },
    'PUT',
  );
  item.title = updated.title;
  item.revision = updated.revision;
  if (session.value?.id === item.id) {
    openStory().title = updated.title;
    openStory().revision = updated.revision;
  }
}
/** A title the user clicked among the advisor's candidates. */
async function applyAdvisorTitle(title: string) {
  const current = session.value;
  if (!current || savingSessionTitle.value) return;
  savingSessionTitle.value = true;
  try {
    await guarded(() => renameSession(sessions.value.find((s) => s.id === current.id) || current, title));
  } finally {
    savingSessionTitle.value = false;
  }
}
/** An advisor option only fills the main input; the user still edits and sends it. */
function useAdvisorInput(text: string) {
  input.value = input.value.trim()
    ? `${input.value.trimEnd()}
${text}`
    : text;
  storyView.value = 'dialogue';
  if (rightOverlay.value) layout.value.rightCollapsed = true;
  nextTick(() => composer.value?.querySelector('textarea')?.focus());
}
function atLatest() {
  const el = chat.value;
  return !!el && el.scrollHeight - el.scrollTop - el.clientHeight <= 48;
}
function trackScroll() {
  if (chat.value && !readingPinned.value) followLatest.value = atLatest();
}
function pauseFollow(event: WheelEvent) {
  readingPinned.value = false;
  if (event.deltaY < 0 && chat.value && chat.value.scrollHeight > chat.value.clientHeight)
    followLatest.value = false;
}
async function navigateMessage(id: string) {
  pendingReplyStart.value = '';
  storyView.value = 'dialogue';
  readingPinned.value = true;
  followLatest.value = false;
  await nextTick();
  await dialogue.value?.jumpTo(id);
}
async function locateCacheBreakpoint() {
  if (cacheBreakpointId.value) await navigateMessage(cacheBreakpointId.value);
  else {
    storyView.value = 'dialogue';
    dialogue.value?.releaseAnchor();
    readingPinned.value = true;
    followLatest.value = false;
    await nextTick();
    chat.value?.scrollTo({ top: 0, behavior: 'instant' });
  }
}
async function jumpBoundary(edge: 'start' | 'end') {
  readingPinned.value = true;
  followLatest.value = false;
  await nextTick();
  await dialogue.value?.jumpBoundary(edge);
  // Reaching the end of the newest message is reading the latest: resume following
  // so the floating jump button does not linger over a fully read story.
  if (edge === 'end' && atLatest()) await scroll(true);
}
watch(storyView, (view) => {
  if (view === 'dialogue' && pendingReplyStart.value) void navigateMessage(pendingReplyStart.value);
});
async function scroll(force = false) {
  if (force) {
    readingPinned.value = false;
    dialogue.value?.releaseAnchor();
    followLatest.value = true;
  }
  await nextTick();
  if (chat.value && followLatest.value)
    chat.value.scrollTo({ top: chat.value.scrollHeight, behavior: 'instant' });
}
const regenerationNote = computed({
  get: () => (retryableTurn.value ? retryNote.value : regenerateNote.value),
  set: (value: string) => {
    if (retryableTurn.value) retryNote.value = value;
    else regenerateNote.value = value;
  },
});
const regenerationIncludeReply = computed({
  get: () => (retryableTurn.value ? retryIncludeReply.value : regenerateIncludeReply.value),
  set: (value: boolean) => {
    if (retryableTurn.value) retryIncludeReply.value = value;
    else regenerateIncludeReply.value = value;
  },
});
const canReferenceReply = computed(() =>
  retryableTurn.value ? retryableTurn.value.request.mode === 'regenerate' : !!latestReply.value,
);
async function closeRewriteOptions() {
  rewriteExpanded.value = false;
  await nextTick();
  rewriteToggle.value?.focus({ preventScroll: true });
}
async function regenerateReply() {
  if (
    busy.value ||
    variablePreferenceSaving.value ||
    editingMessageId.value ||
    activeTurn.value?.status === 'awaiting_review'
  )
    return;
  if (retryableTurn.value) await retryTurn();
  else if (latestReply.value) await send('regenerate', latestReply.value, '', regenerateNote.value);
}
async function retryTurn() {
  if (!retryableTurn.value || busy.value || variablePreferenceSaving.value || editingMessageId.value) return;
  const turn = retryableTurn.value;
  const resumeFullAgent =
    session.value?.agent_mode === 'full_agent' &&
    turn.resumable &&
    turn.request.expected_revision === openStory().revision &&
    allowEdit.value === !!turn.request.allow_content_edit &&
    updateVariables.value === !!turn.request.update_variables &&
    retryIncludeReply.value === !!turn.request.regenerate_include_reply;
  await submitStoryTurn({
    ...turn.request,
    ...storyTurnOptions(),
    regenerate_note: retryNote.value.trim(),
    regenerate_include_reply: retryIncludeReply.value,
    resume_turn_id: resumeFullAgent ? turn.id : null,
  });
}
// Imported and blank cards both open in the shelf editor once the library reloads.
async function addLibraryCard(request: () => Promise<CardRecord>) {
  await guarded(async () => {
    selected.value = await request();
    await load();
    sidebarSection.value = 'cards';
    pageMode.value = 'card';
    panel.value = '';
    if (window.innerWidth < 760) layout.value.leftCollapsed = true;
  });
}
function importCard(file: File) {
  const form = new FormData();
  form.append('file', file);
  return addLibraryCard(() => api('/cards/import', form));
}
function createCard() {
  return addLibraryCard(() => api('/cards', {}));
}
async function refreshCardConversations() {
  cardConversations.value = await api('/card-author/conversations');
}
function openCardAgent(id: string) {
  abandonStoryLoad();
  editorConversationId.value = id;
  sidebarSection.value = 'editor';
  pageMode.value = 'editor';
  panel.value = '';
  localStorage.setItem('tavern-active-card-conversation', id);
  if (window.innerWidth < 760) layout.value.leftCollapsed = true;
  void refreshCardConversations();
}
async function newCardConversation() {
  await guarded(async () => {
    const value = await api('/card-author/conversations', {});
    openCardAgent(value.id);
  });
}
async function manageCardAgent() {
  await guarded(async () => {
    await refreshCardConversations();
    const remembered = localStorage.getItem('tavern-active-card-conversation');
    const chosen = cardConversations.value.find((c) => c.id === remembered) || cardConversations.value[0];
    if (chosen) openCardAgent(chosen.id);
    else {
      abandonStoryLoad();
      editorConversationId.value = '';
      sidebarSection.value = 'editor';
      pageMode.value = 'editor';
      panel.value = '';
      if (window.innerWidth < 760) layout.value.leftCollapsed = true;
    }
  });
}
async function cardConversationDeleted(id: string) {
  sessionStorage.removeItem(`tavern-editor-input-${id}`);
  localStorage.removeItem(`tavern-editor-model-${id}`);
  localStorage.removeItem(`tavern-editor-effort-${id}`);
  if (localStorage.getItem('tavern-active-card-conversation') === id)
    localStorage.removeItem('tavern-active-card-conversation');
  const wasCurrent = editorConversationId.value === id;
  if (wasCurrent) editorConversationId.value = '';
  await refreshCardConversations();
  // A completed deletion must not navigate away from a view opened meanwhile.
  if (wasCurrent && !editorConversationId.value && pageMode.value === 'editor') {
    if (cardConversations.value.length) openCardAgent(cardConversations.value[0].id);
    else {
      sidebarSection.value = 'editor';
    }
  }
}
async function deleteCardConversation(item: CardConversationSummary) {
  if (deletingCardConversationId.value) return;
  closeDeleteBubble(false);
  deletingCardConversationId.value = item.id;
  try {
    await guarded(async () => {
      await api(`/card-author/conversations/${item.id}`, { expected_revision: item.revision }, 'DELETE');
      await cardConversationDeleted(item.id);
    });
  } finally {
    deletingCardConversationId.value = '';
  }
}
async function copyLibraryCard(card: CardRecord) {
  await guarded(async () => {
    const copied = await api(`/cards/${card.id}/copy`, {});
    await load();
    showCard(copied);
  });
}
// Switching the Agent workspace keeps the editor's mount key so its composer survives.
function selectWorkspace(id: string) {
  const card = selected.value;
  if (!card) return;
  selected.value = { ...card, _editorKey: card._editorKey || card._workspaceId || card.id, _workspaceId: id };
}
async function cardSaved(card: CardRecord) {
  // Saving an Agent workspace, including Save As, must not remount its composer.
  selected.value =
    card?._workspaceId && selected.value
      ? { ...card, _editorKey: selected.value._editorKey || selected.value._workspaceId || selected.value.id }
      : card;
  await load();
}
function editSessionLore() {
  const card = sessionCard.value;
  if (!card) throw new Error('对话角色卡尚未载入');
  selected.value = { ...card, document: openStory().snapshot.content?.document || card.document };
  pageMode.value = 'session-lore';
}
function sessionLoreSaved(value: any) {
  session.value = value;
  activeTurn.value = value.turns.find((turn: any) => turn.status === 'awaiting_review') || null;
  pageMode.value = '';
  if (activeTurn.value) {
    const id = currentTurn().id;
    void guarded(async () => {
      activeTurn.value = await viewApi(`/turns/${id}`);
    });
  }
}
watch(pageMode, (value) => {
  if (value) abandonStoryLoad();
});
function showCard(card: CardRecord) {
  // Explicit navigation starts a fresh view, even after Save As retained an
  // earlier editor instance. The draft itself is restored by workspace ID.
  selected.value = { ...card, _editorKey: crypto.randomUUID() };
  sidebarSection.value = 'cards';
  pageMode.value = 'card';
  panel.value = '';
  if (window.innerWidth < 760) layout.value.leftCollapsed = true;
}
function startNewStory() {
  if (busy.value) return;
  sidebarSection.value = 'sessions';
  pageMode.value = 'start';
  panel.value = '';
  if (window.innerWidth < 760) layout.value.leftCollapsed = true;
}
// Repeated selection toggles the list without reopening the current workspace.
function openSidebarSection(section: 'sessions' | 'cards' | 'worldbooks' | 'editor', toggle = false) {
  if (toggle && sidebarSection.value === section) {
    void toggleLeftSidebar();
    return;
  }
  if (layout.value.leftCollapsed) void toggleLeftSidebar();
  if (section === 'sessions') sidebarSection.value = 'sessions';
  else if (section === 'cards') manageCards();
  else if (section === 'worldbooks') sidebarSection.value = 'worldbooks';
  else void manageCardAgent();
}
async function showWorldbook(item: WorldbookSummary) {
  await guarded(async () => {
    selectedWorldbook.value = await api(`/worldbooks/${item.id}`);
    sidebarSection.value = 'worldbooks';
    pageMode.value = 'worldbook';
    panel.value = '';
    if (window.innerWidth < 760) layout.value.leftCollapsed = true;
  });
}
async function refreshWorldbooks() {
  worldbooks.value = await api('/worldbooks');
}
async function createWorldbook(name: string, book: JsonObject) {
  await guarded(async () => {
    const created: Worldbook = await api('/worldbooks', { name, book });
    await refreshWorldbooks();
    await showWorldbook({ ...created, entries: 0 });
  });
}
function newWorldbook() {
  const names = new Set(worldbooks.value.map((book) => book.name));
  let name = '新世界书',
    index = 2;
  while (names.has(name)) name = `新世界书 ${index++}`;
  void createWorldbook(name, { entries: [] });
}
async function importWorldbook(file: File) {
  await guarded(async () => {
    let book: JsonObject;
    try {
      book = JSON.parse(await file.text());
    } catch {
      throw new Error(`${file.name} 不是有效的 JSON 文件`);
    }
    // ST World Info files carry no name; V2 books may name themselves.
    const named = typeof book?.name === 'string' && book.name.trim() ? book.name.trim() : '';
    await createWorldbook(named || file.name.replace(/\.json$/i, '') || '导入的世界书', book);
  });
}
async function worldbookSaved(book: Worldbook) {
  selectedWorldbook.value = book;
  await guarded(refreshWorldbooks);
}
async function deleteWorldbook(item: WorldbookSummary) {
  closeDeleteBubble(false);
  await guarded(async () => {
    await api(`/worldbooks/${item.id}`, {}, 'DELETE');
    if (selectedWorldbook.value?.id === item.id) {
      selectedWorldbook.value = null;
      if (pageMode.value === 'worldbook') pageMode.value = '';
    }
    await refreshWorldbooks();
  });
}
function manageCards() {
  abandonStoryLoad();
  viewScope.enter();
  events?.close();
  sidebarSection.value = 'cards';
  session.value = null;
  pageMode.value = '';
}
async function cardStarted(s: any) {
  sidebarSection.value = 'sessions';
  const opening = openSession(s.id, true, s);
  const token = viewScope.current();
  if (storyLoad.value) storyLoad.value.name = s.title || s.name || '';
  try {
    const list = await api('/sessions');
    if (viewScope.owns(token)) sessions.value = list;
  } catch (e: any) {
    if (viewScope.owns(token)) error.value = e.message;
  }
  await opening;
}
function setEffort(value: string) {
  quickEffort.value = value;
  if (session.value) localStorage.setItem(`tavern-effort-${openStory().id}`, value);
}
function storyTurnOptions() {
  return {
    operation_id: crypto.randomUUID(),
    expected_revision: openStory().revision,
    allow_content_edit: allowEdit.value,
    update_variables: updateVariables.value,
    reasoning_effort: quickEffort.value === 'inherit' ? null : quickEffort.value,
  };
}
async function send(
  mode: 'send' | 'regenerate' = 'send',
  message?: StoryMessageRecord,
  text?: string,
  note = '',
) {
  if (!session.value) return;
  const value = text ?? input.value;
  if (mode === 'send' && !value.trim()) return;
  const submitted = await submitStoryTurn({
    ...storyTurnOptions(),
    text: value,
    mode,
    message_id: message?.id,
    regenerate_note: note,
    regenerate_include_reply: mode === 'regenerate' && regenerateIncludeReply.value,
  });
  if (submitted && mode === 'send' && input.value === value) input.value = '';
}
async function submitStoryTurn(request: TurnRequest): Promise<boolean> {
  if (!session.value || busy.value || sceneSubmitting.value || variablePreferenceSaving.value) return false;
  if (modelSaving.value || contextBusy.value || !session.value.channel_origin || !channelModelsReady.value) {
    notify(
      contextBusy.value
        ? '请先完成或取消上下文整理。'
        : !session.value.channel_origin
          ? '请先确认此对话的渠道归属。'
          : '请先配置此渠道的模型连接。',
    );
    return false;
  }
  const sid = openStory().id,
    token = viewScope.current();
  busy.value = true;
  error.value = '';
  pendingInput.value = null;
  pendingLore.value = [];
  try {
    const turn: TurnRecord = await api(`/sessions/${sid}/turns`, request);
    if (!viewScope.owns(token)) return false;
    activeTurn.value = turn;
    const pendingText = request.mode === 'regenerate' ? request.replacement_text : request.text;
    if (pendingText) pendingInput.value = { raw: pendingText };
    regenerateNote.value = '';
    regenerateIncludeReply.value = false;
    rewriteExpanded.value = false;
    workCount.value += 1;
    void scroll(true);
    observe(turn.id);
    return true;
  } catch (e: any) {
    if (viewScope.owns(token)) {
      error.value = e.message;
      busy.value = false;
    }
    return false;
  }
}

function observe(id: string) {
  events?.close();
  const sid = openStory().id,
    token = viewScope.current();
  // Rebuild from sequence zero on reopen instead of appending replay to a draft snapshot.
  const ownsTurn = () => viewScope.owns(token) && activeTurn.value?.id === id;
  draft.value = '';
  trace.value = [];
  events = observeTurn(
    id,
    ownsTurn,
    (event) => {
      if (event.kind === 'text' || event.kind === 'text_reset') {
        draft.value = turnDraft(draft.value, event);
        void scroll();
      } else {
        trace.value.push(event);
        if (event.kind === 'full_agent_prefix_debug') notify(event.payload.message);
        if (event.kind === 'input_ready') {
          pendingInput.value = event.payload.message;
          pendingLore.value = event.payload.lore_injections || [];
          void scroll();
        }
        if (event.kind === 'scene_refresh_ready' && event.payload.purpose === 'memory') {
          delete openStory().snapshot.memory_warning;
          memoryWarning.value = '';
          error.value = '';
        }
        if (event.kind === 'error') error.value = turnError(event.payload.message);
        if (event.kind === 'memory_failed') openStory().snapshot.memory_warning = event.payload.message;
        if (event.kind === 'memory_ready') delete openStory().snapshot.memory_warning;
        if (event.kind === 'memory_records_discarded') memoryDiscardNotice.value = event.payload.message;
      }
    },
    async () => {
      try {
        const [turn, updated]: [TurnRecord, Session] = await Promise.all([
          api(`/turns/${id}`),
          api(`/sessions/${sid}`),
        ]);
        if (!ownsTurn()) return;
        const following = followLatest.value,
          top = chat.value?.scrollTop || 0;
        const completedReply =
          turn.status === 'committed' && !turn.request?.scene_refresh
            ? updated.messages
                .slice()
                .reverse()
                .find((message) => message.role === 'assistant')
            : null;
        if (following && completedReply) {
          readingPinned.value = true;
          followLatest.value = false;
        }
        activeTurn.value = turn;
        draft.value = turn.draft;
        busy.value = false;
        session.value = updated;
        await nextTick();
        if (!ownsTurn()) return;
        if (following && completedReply) {
          if (storyView.value === 'dialogue') await navigateMessage(completedReply.id);
          else pendingReplyStart.value = completedReply.id;
        } else if (following) await scroll();
        else {
          chat.value?.scrollTo({ top, behavior: 'instant' });
          followLatest.value = false;
        }
        await load();
        await refreshContextStats();
      } catch (e: any) {
        if (ownsTurn()) {
          error.value = e.message;
          busy.value = false;
        }
      }
    },
  );
}

async function saveMemoryRecord(change: any) {
  if (sceneSaving.value || busy.value) return;
  sceneSaving.value = 'memory';
  try {
    await guarded(async () => {
      session.value = await viewApi(
        `/sessions/${openStory().id}/memory/records`,
        { ...change, expected_revision: openStory().revision },
        'PUT',
      );
      await refreshContextStats();
    });
  } finally {
    sceneSaving.value = null;
  }
}
async function locateSource(id: string, excerpt: string) {
  const message = session.value?.messages.find((m) => m.id === id);
  if (!message) return;
  panel.value = '';
  storyView.value = 'dialogue';
  followLatest.value = false;
  sourceHighlight.value = { id, excerpt, fallback: false };
  await navigateMessage(id);
  dialogue.value?.releaseAnchor();
  const target = Array.from(chat.value?.querySelectorAll<HTMLElement>('[data-message-id]') || []).find(
    (node) => node.dataset.messageId === id,
  );
  target?.scrollIntoView({ block: 'center' });
  chat.value?.querySelectorAll('mark.source-sentence-highlight').forEach((mark) => {
    const parent = mark.parentElement;
    mark.replaceWith(...Array.from(mark.childNodes));
    parent?.normalize();
  });
  const prose = target?.querySelector<HTMLElement>('.prose');
  if (excerpt && (!prose || !highlightSource(prose, excerpt))) {
    sourceHighlight.value = { id, excerpt, fallback: true };
    await nextTick();
    target?.querySelector('.source-excerpt')?.scrollIntoView({ block: 'center' });
  }
  followLatest.value = false;
}
async function refreshScene(kind: 'memory') {
  if (!session.value || busy.value || sceneSubmitting.value) return;
  memoryRebuildEstimate.value = null;
  sceneSubmitting.value = kind;
  try {
    await guarded(async () => {
      memoryRebuildEstimate.value = await viewApi(`/sessions/${openStory().id}/memory/rebuild-estimate`);
      panel.value = 'memory-rebuild';
    });
  } finally {
    sceneSubmitting.value = null;
  }
}
async function startSceneRefresh(kind: 'memory') {
  if (
    !session.value ||
    busy.value ||
    sceneSubmitting.value ||
    editingMessageId.value ||
    activeTurn.value?.status === 'awaiting_review'
  )
    return;
  sceneSubmitting.value = kind;
  try {
    await guarded(async () => {
      activeTurn.value = await viewApi(`/sessions/${openStory().id}/scene/${kind}/regenerate`, {
        operation_id: crypto.randomUUID(),
        expected_revision: openStory().revision,
      });
      panel.value = '';
      busy.value = true;
      draft.value = '';
      trace.value = [];
      observe(currentTurn().id);
    });
  } finally {
    sceneSubmitting.value = null;
  }
}
async function stop() {
  if (activeTurn.value) await guarded(() => api(`/turns/${currentTurn().id}/cancel`, {}));
}
async function review(accept: boolean) {
  const sid = openStory().id,
    token = viewScope.current();
  await guarded(async () => {
    await api(`/turns/${currentTurn().id}/review`, { accept });
    if (viewScope.owns(token)) await openSession(sid);
  });
}
function beginPendingEdit() {
  if (!retryableTurn.value || busy.value || editingMessageId.value || !visiblePendingInput.value) return;
  pauseForMessageEdit();
  editingMessageId.value = '__pending__';
  editingMessageText.value = visiblePendingInput.value.raw;
}
async function savePendingEdit() {
  if (!retryableTurn.value) return;
  const previous = retryableTurn.value.request;
  await submitInputEdit({
    ...previous,
    ...(previous.mode === 'regenerate'
      ? { replacement_text: editingMessageText.value }
      : { text: editingMessageText.value }),
  });
}
function pauseForMessageEdit() {
  rewriteExpanded.value = false;
  const top = chat.value?.scrollTop || 0;
  const token = viewScope.current();
  editingWasFollowing = followLatest.value;
  followLatest.value = false;
  dialogue.value?.releaseAnchor();
  void nextTick(() =>
    requestAnimationFrame(() => {
      if (viewScope.owns(token) && editingMessageId.value && chat.value) chat.value.scrollTop = top;
    }),
  );
}
function beginMessageEdit(message: StoryMessageRecord) {
  if (
    !editableMessageIds.value.has(message.id) ||
    busy.value ||
    switchingVersion.value ||
    editingMessageId.value ||
    activeTurn.value?.status === 'awaiting_review'
  )
    return;
  pauseForMessageEdit();
  editingMessageId.value = message.id;
  editingMessageText.value = message.raw;
}
function cancelMessageEdit() {
  const previous = editingMessageId.value;
  editingMessageId.value = '';
  editingMessageText.value = '';
  savingMessageEdit.value = false;
  if (previous)
    void nextTick(() => {
      if (editingMessageId.value) return;
      if (chat.value)
        followLatest.value =
          editingWasFollowing &&
          chat.value.scrollHeight - chat.value.scrollTop - chat.value.clientHeight <= 48;
      document
        .querySelector<HTMLButtonElement>(`[data-message-id="${previous}"] button[aria-label="编辑"]`)
        ?.focus({ preventScroll: true });
    });
}
const switchingVersion = ref(false);
async function switchReplyVersion(message: StoryMessageRecord, offset: number) {
  if (
    busy.value ||
    switchingVersion.value ||
    editingMessageId.value ||
    activeTurn.value?.status === 'awaiting_review'
  )
    return;
  const target = message.versions?.[(message.version_index ?? 0) + offset];
  if (!target || message.id !== session.value?.head) return;
  switchingVersion.value = true;
  try {
    await guarded(async () => {
      session.value = await viewApi(
        `/sessions/${openStory().id}/messages/${message.id}/version`,
        { head: target, expected_revision: openStory().revision },
        'PUT',
      );
      activeTurn.value = null;
      pendingInput.value = null;
      pendingLore.value = [];
      draft.value = '';
      error.value = '';
      await refreshContextStats();
    });
  } finally {
    switchingVersion.value = false;
  }
}
async function saveMessageEdit(message: StoryMessageRecord) {
  if (!editableMessageIds.value.has(message.id) || !latestReply.value) return;
  await submitInputEdit({
    ...storyTurnOptions(),
    mode: 'regenerate',
    message_id: latestReply.value.id,
    replacement_text: editingMessageText.value,
  });
}
async function submitInputEdit(request: TurnRequest) {
  if (
    !editingMessageText.value.trim() ||
    busy.value ||
    switchingVersion.value ||
    savingMessageEdit.value ||
    activeTurn.value?.status === 'awaiting_review'
  )
    return;
  const token = viewScope.current();
  savingMessageEdit.value = true;
  try {
    const submitted = await submitStoryTurn({
      ...request,
      ...storyTurnOptions(),
      resume_turn_id: null,
      regenerate_note: '',
      regenerate_include_reply: false,
    });
    if (submitted) cancelMessageEdit();
  } finally {
    if (viewScope.owns(token)) savingMessageEdit.value = false;
  }
}
async function inspectContext() {
  await guarded(async () => {
    preview.value = await viewApi(`/sessions/${openStory().id}/preview`, {
      text: input.value,
      update_variables: updateVariables.value,
      allow_content_edit: allowEdit.value,
    });
    contextStats.value = preview.value;
    contextEstimateError.value = '';
    panel.value = 'context';
  });
}
async function exportNextMessages() {
  if (!session.value || busy.value || previewMessagesBusy.value) return;
  previewMessagesBusy.value = true;
  try {
    await guarded(async () => {
      const sid = openStory().id;
      const messages = await viewApi(`/sessions/${sid}/messages-preview`, {
        text: input.value,
        update_variables: updateVariables.value,
        allow_content_edit: allowEdit.value,
      });
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(messages, null, 2)], { type: 'application/json;charset=utf-8' }),
      );
      const link = document.createElement('a');
      link.href = url;
      link.download = `messages-preview-${sid}.json`;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
      notify('已导出完整 messages 数组。只读预览未运行神经召回，实际发送时的世界书命中可能不同。');
    });
  } finally {
    previewMessagesBusy.value = false;
  }
}
async function showForkPoints() {
  if (
    !session.value ||
    busy.value ||
    editingMessageId.value ||
    activeTurn.value?.status === 'awaiting_review'
  )
    return;
  await guarded(async () => {
    const messages: BranchMessage[] = await viewApi(`/sessions/${openStory().id}/branches`);
    forkPoints.value = messages.filter((message) => message.role === 'assistant');
    panel.value = 'fork-select';
  });
}
function askFork(message: BranchMessage | StoryMessageRecord | null, fromPicker = false) {
  if (!message || busy.value || editingMessageId.value || activeTurn.value?.status === 'awaiting_review')
    return;
  forkMessage.value = message;
  forkFromPicker.value = fromPicker;
  panel.value = 'fork';
}
async function createFork() {
  const fork = forkMessage.value;
  if (!session.value || !fork || creatingFork.value) return;
  creatingFork.value = true;
  try {
    await guarded(async () => {
      const originalId = openStory().id,
        effort = quickEffort.value;
      const created = await api(`/sessions/${originalId}/fork`, {
        message_id: fork.id,
        expected_revision: openStory().revision,
      });
      localStorage.setItem(`tavern-effort-${created.id}`, effort);
      panel.value = '';
      forkMessage.value = null;
      input.value = '';
      regenerateNote.value = '';
      await load();
      await openSession(created.id);
    });
  } finally {
    creatingFork.value = false;
  }
}
async function saveCacheBreakpoint(
  automatic: boolean,
  messageId: string | null = null,
  interval?: number,
  offset?: number,
) {
  if (
    !session.value ||
    busy.value ||
    cacheBreakpointBusy.value ||
    editingMessageId.value ||
    activeTurn.value?.status === 'awaiting_review' ||
    switchingVersion.value
  )
    return;
  cacheBreakpointBusy.value = true;
  try {
    await guarded(async () => {
      const sid = openStory().id;
      const updated = await viewApi(
        `/sessions/${sid}/cache-breakpoint`,
        {
          automatic,
          message_id: automatic ? null : messageId,
          expected_revision: openStory().revision,
          follow_interval: interval,
          reply_offset: offset,
        },
        'PUT',
      );
      const story = openStory();
      for (const field of [
        'revision',
        'cache_breakpoint_auto',
        'cache_breakpoint_message',
        'cache_breakpoint_effective_id',
        'cache_breakpoint_follow',
        'cache_breakpoint_reason',
      ] as const)
        Object.assign(story, { [field]: updated[field] });
      const listed = sessions.value.find((item) => item.id === sid);
      if (listed) listed.revision = updated.revision;
      await refreshContextStats();
    });
  } finally {
    cacheBreakpointBusy.value = false;
  }
}
function toggleCacheBreakpointFollow() {
  const automatic = !session.value?.cache_breakpoint_auto;
  void saveCacheBreakpoint(automatic, automatic ? null : cacheBreakpointId.value);
}
async function saveCacheRetention(value: string) {
  if (busy.value || cacheBreakpointBusy.value || !writerCacheRetention.value.enabled) return;
  const scope = isFullAgent.value ? 'full_agent' : 'half_agent';
  const path = isFullAgent.value ? 'model.cache.retention' : 'foreground.cache.retention';
  cacheBreakpointBusy.value = true;
  try {
    await guarded(async () => {
      settings.value = await api(
        `/settings/${scope}`,
        {
          expected_revision: loadedSettings().revisions[scope],
          changes: { [path]: value },
        },
        'PATCH',
      );
      notify('缓存时效已保存，从下一次请求生效。');
    });
  } finally {
    cacheBreakpointBusy.value = false;
  }
}
async function saveOptions(draft: SessionOptionsDraft) {
  await guarded(async () => {
    session.value = await viewApi(
      `/sessions/${openStory().id}/options`,
      { ...draft, turn_preference: draft.turn_preference || '', expected_revision: openStory().revision },
      'PUT',
    );
    showOptionsNotice('已保存');
  });
}
async function saveRecipe(recipe: JsonObject) {
  if (savingRecipe.value) return;
  savingRecipe.value = true;
  try {
    await guarded(async () => {
      session.value = await viewApi(
        `/sessions/${openStory().id}/recipe`,
        { recipe, expected_revision: openStory().revision },
        'PUT',
      );
      panel.value = '';
      await refreshContextStats();
    });
  } finally {
    savingRecipe.value = false;
  }
}
async function resetRecipe() {
  if (!session.value || savingRecipe.value || busy.value || activeTurn.value?.status === 'awaiting_review')
    return;
  savingRecipe.value = true;
  try {
    await guarded(async () => {
      session.value = await viewApi(
        `/sessions/${openStory().id}/recipe`,
        { use_global: true, expected_revision: openStory().revision },
        'PUT',
      );
      showOptionsNotice('已采用当前全局编排');
      await refreshContextStats();
    });
  } finally {
    savingRecipe.value = false;
  }
}
async function frameAction(
  method: string,
  args: any,
  resolve: (v: any) => void,
  reject: (e: any) => void,
  onStream: (chunk: string) => void,
) {
  try {
    if (method === 'input') {
      input.value = String(args.text || '');
      resolve(null);
      return;
    }
    if (method === 'log') {
      trace.value.push({ kind: 'script_log', payload: args });
      resolve(null);
      return;
    }
    if (method === 'cancel') {
      await stop();
      resolve(true);
      return;
    }
    if (method === 'generate') {
      if (busy.value) throw Error('当前回合正在运行');
      const turn = await api(`/sessions/${openStory().id}/turns`, {
        text: args.user_input || '继续',
        generate: args,
        expected_revision: openStory().revision,
        operation_id: crypto.randomUUID(),
        reasoning_effort: quickEffort.value === 'inherit' ? null : quickEffort.value,
      });
      activeTurn.value = turn;
      busy.value = true;
      draft.value = '';
      void scroll(true);
      observe(turn.id);
      const stream = args.should_stream ? new EventSource(`/api/turns/${turn.id}/events`) : null;
      if (stream) {
        stream.onmessage = (e) => {
          const item = JSON.parse(e.data);
          if (item.kind === 'text') onStream(item.payload.text);
        };
        stream.addEventListener('end', () => stream.close());
      }
      const poll = async (): Promise<any> => {
        const t = await api(`/turns/${turn.id}`);
        if (['running', 'queued'].includes(t.status)) {
          await new Promise((r) => setTimeout(r, 300));
          return poll();
        }
        if (t.status !== 'committed') throw Error(t.error || '回合未提交');
        return t.draft;
      };
      try {
        resolve(await poll());
      } finally {
        stream?.close();
      }
      return;
    }
    const updated: Session = await viewApi(`/sessions/${openStory().id}/interaction`, {
      method,
      args,
      expected_revision: openStory().revision,
      operation_id: crypto.randomUUID(),
      allow_content_edit: allowEdit.value,
    });
    session.value = updated;
    const review = updated.turns.find((t) => t.status === 'awaiting_review');
    if (review) activeTurn.value = await viewApi(`/turns/${review.id}`);
    resolve(null);
  } catch (e: any) {
    if (!(e instanceof StaleView)) error.value = e.message;
    reject(e.message);
  }
}
onMounted(() => {
  mobileSidebarQuery.addEventListener('change', syncMobileSidebar);
  rightOverlayQuery.addEventListener('change', syncRightOverlay);
  document.addEventListener('pointerdown', closeDeleteOnOutside);
  document.addEventListener('scroll', closeDeleteOnViewportChange, true);
  window.addEventListener('resize', closeDeleteOnViewportChange);
  guarded(async () => {
    await load();
    const last = localStorage.getItem('tavern-session');
    if (sidebarSection.value === 'editor') await manageCardAgent();
    else if (sessions.value.some((s) => s.id === last)) await openSession(last!);
    const status: LocalModelStatus = await api('/local-models');
    const enabled = [
      ...(activeSettings.value.retrieval?.channels?.includes('embedding') ? ['embedding' as const] : []),
      ...(activeSettings.value.local_runtime?.reranker_enabled ? ['reranker' as const] : []),
    ].filter(
      (purpose) =>
        !status.models[purpose].ready &&
        !status.jobs.some(
          (job) => job.kind === `warmup:${purpose}` && ['queued', 'running'].includes(job.status),
        ),
    );
    if (enabled.length && localStorage.getItem('tavern-warmup-startup') !== status.startup_id) {
      startupModels.value = { ...status, enabled };
      panel.value = 'warmup';
    }
  });
});
onBeforeUnmount(() => {
  mobileSidebarQuery.removeEventListener('change', syncMobileSidebar);
  rightOverlayQuery.removeEventListener('change', syncRightOverlay);
  clearOptionsNotice();
  clearTimeout(contextRefreshTimer);
  document.removeEventListener('pointerdown', closeDeleteOnOutside);
  document.removeEventListener('scroll', closeDeleteOnViewportChange, true);
  window.removeEventListener('resize', closeDeleteOnViewportChange);
  events?.close();
  stopRightResize?.();
  composerObserver?.disconnect();
});
useTimedError(error);
</script>

<template>
  <Teleport to="body"
    ><div v-if="notification" class="app-notification" role="alert">{{ notification }}</div></Teleport
  >
  <div class="shell" :style="readingStyle" :inert="!!panel">
    <aside
      id="primary-sidebar"
      class="sidebar"
      :class="{ collapsed: layout.leftCollapsed }"
      :inert="rightDrawerOpen"
      @keydown.esc="mobileSidebar && !layout.leftCollapsed && toggleLeftSidebar()"
    >
      <nav class="rail" aria-label="主导航">
        <a
          class="brand"
          href="#"
          title="Terrarium Tavern"
          aria-label="Terrarium Tavern"
          @click.prevent="startNewStory"
          ><Icon name="brand" :size="24" :stroke="1.5"
        /></a>
        <div class="rail-sections" role="tablist" aria-label="酒馆管理">
          <button
            role="tab"
            class="rail-tab"
            :aria-selected="sidebarSection === 'sessions'"
            :aria-expanded="sidebarSection === 'sessions' && !layout.leftCollapsed"
            aria-controls="primary-sidebar-panel"
            :class="{ active: sidebarSection === 'sessions' }"
            @click="openSidebarSection('sessions', true)"
          >
            <Icon name="chat" :size="20" /><span>会话</span>
          </button>
          <button
            role="tab"
            class="rail-tab"
            :aria-selected="sidebarSection === 'cards'"
            :aria-expanded="sidebarSection === 'cards' && !layout.leftCollapsed"
            aria-controls="primary-sidebar-panel"
            :class="{ active: sidebarSection === 'cards' }"
            @click="openSidebarSection('cards', true)"
          >
            <Icon name="cards" :size="20" /><span>角色卡</span>
          </button>
          <button
            role="tab"
            class="rail-tab"
            :aria-selected="sidebarSection === 'worldbooks'"
            :aria-expanded="sidebarSection === 'worldbooks' && !layout.leftCollapsed"
            aria-controls="primary-sidebar-panel"
            :class="{ active: sidebarSection === 'worldbooks' }"
            @click="openSidebarSection('worldbooks', true)"
          >
            <Icon name="book" :size="20" /><span>世界书</span>
          </button>
          <button
            role="tab"
            class="rail-tab"
            :aria-selected="sidebarSection === 'editor'"
            :aria-expanded="sidebarSection === 'editor' && !layout.leftCollapsed"
            aria-controls="primary-sidebar-panel"
            :class="{ active: sidebarSection === 'editor' }"
            @click="openSidebarSection('editor', true)"
          >
            <Icon name="quill" :size="20" /><span>制卡</span>
          </button>
        </div>
        <div class="rail-bottom sidebar-bottom">
          <button class="rail-button" title="用户角色" aria-label="用户角色" @click="pageMode = 'personas'">
            <Icon name="user" :size="19" /></button
          ><button class="rail-button" title="阅读排版" aria-label="阅读排版" @click="panel = 'layout'">
            <Icon name="type" :size="19" /></button
          ><button class="rail-button" title="设置" aria-label="设置" @click="panel = 'settings'">
            <Icon name="cog" :size="19" /></button
          ><button
            class="rail-button"
            title="关于 Terrarium Tavern"
            aria-label="关于 Terrarium Tavern"
            @click="panel = 'about'"
          >
            <Icon name="info" :size="19" /></button
          ><button
            ref="leftSidebarClose"
            class="rail-button sidebar-collapse"
            :title="layout.leftCollapsed ? '展开左侧栏' : '折叠左侧栏'"
            @click="toggleLeftSidebar"
            :aria-label="layout.leftCollapsed ? '展开左侧栏' : '折叠左侧栏'"
            :aria-expanded="!layout.leftCollapsed"
            aria-controls="primary-sidebar"
          >
            <Icon name="sidebar" :size="19" />
          </button>
        </div>
      </nav>
      <div id="primary-sidebar-panel" class="sidebar-panel">
        <div class="panel-brand">Terrarium Tavern</div>
        <SessionList
          v-if="sidebarSection === 'sessions'"
          :sessions="sessions"
          :active-id="session?.id"
          :portraits="sessionPortraits"
          :busy="busy"
          :saving="savingSessionTitle"
          :rename="renameFromList"
          @new-story="startNewStory"
          @open="(id) => guarded(() => openSession(id))"
          @delete="(item, event) => askDeleteConversation({ kind: 'session', item }, event)"
          @invalid="(message) => (error = message)"
        />
        <CardList
          v-else-if="sidebarSection === 'cards'"
          :cards="cards"
          :orphan-drafts="orphanDrafts"
          :active-id="pageMode === 'card' ? selected?.id : undefined"
          @create="createCard"
          @import="importCard"
          @show="showCard"
          @copy="copyLibraryCard"
          @delete="
            (card) => {
              deletingCard = card;
              panel = 'delete-card';
            }
          "
          @open-draft="openCardAgent"
        />
        <WorldbookList
          v-else-if="sidebarSection === 'worldbooks'"
          :worldbooks="worldbooks"
          :active-id="pageMode === 'worldbook' ? selectedWorldbook?.id : undefined"
          @create="newWorldbook"
          @import="importWorldbook"
          @show="showWorldbook"
          @delete="(item, event) => askDeleteConversation({ kind: 'worldbook', item }, event)"
        />
        <CardConversationList
          v-else-if="sidebarSection === 'editor'"
          :conversations="cardConversations"
          :active-id="editorConversationId"
          :deleting="!!deletingCardConversationId"
          @create="newCardConversation"
          @open="openCardAgent"
          @delete="(item, event) => askDeleteConversation({ kind: 'editor', item }, event)"
        />
      </div>
    </aside>

    <button
      v-if="mobileSidebar && !layout.leftCollapsed"
      class="sidebar-scrim"
      type="button"
      aria-label="关闭左侧栏"
      @click="toggleLeftSidebar"
    />

    <main :inert="(mobileSidebar && !layout.leftCollapsed) || rightDrawerOpen">
      <button
        ref="mobileSidebarButton"
        v-show="layout.leftCollapsed"
        class="mobile-nav quiet shell-navigation"
        @click="toggleLeftSidebar"
        aria-label="切换左侧栏"
        :aria-expanded="!layout.leftCollapsed"
        aria-controls="primary-sidebar"
      >
        <Icon name="menu" :size="19" />
      </button>
      <AppNotification :message="error" />
      <AppNotification :message="memoryDiscardNotice" />

      <CardWorkspace
        v-if="pageMode === 'editor' && editorConversationId"
        :key="editorConversationId"
        :conversation-id="editorConversationId"
        :library="cards"
        :worldbooks="worldbooks"
        :model-profile="loadedSettings().shared.card_author"
        :providers="loadedSettings().shared.providers"
        :send-shortcut="loadedSettings().shared.send_shortcut"
        @changed="refreshCardConversations"
        @saved="load"
        @deleted="cardConversationDeleted"
      />
      <section v-else-if="pageMode === 'editor'" class="library home solo" aria-label="制卡对话入口">
        <div class="home-hero">
          <div class="home-emblem" aria-hidden="true"><Icon name="quill" :size="30" :stroke="1.4" /></div>
          <div class="eyebrow">Card Author</div>
          <h1>开始制卡</h1>
          <p class="intro">
            与制卡 agent 一起整理设定、世界书与开场。它只修改独立草稿，保存前由你审阅。{{
              cardConversations.length ? '从左侧继续已有对话，或新建一个。' : ''
            }}
          </p>
          <div class="home-actions">
            <button class="primary" :disabled="busy" @click="newCardConversation">
              <Icon name="plus" :size="17" />新建制卡对话
            </button>
          </div>
        </div>
      </section>
      <CharacterEditor
        v-else-if="pageMode === 'card' && selected"
        :key="selected._editorKey || selected._workspaceId || selected.id"
        :card="selected"
        @saved="cardSaved"
        @open-agent="openCardAgent"
        @workspace-selected="selectWorkspace"
        @back="pageMode = ''"
      /><CharacterEditor
        v-else-if="pageMode === 'session-lore' && selected && session"
        :key="`lore-${session.id}`"
        :card="selected"
        :session="session"
        @session-saved="sessionLoreSaved"
        @back="pageMode = ''"
      />
      <WorldbookEditor
        v-else-if="pageMode === 'worldbook' && selectedWorldbook"
        :key="selectedWorldbook.id"
        :worldbook="selectedWorldbook"
        @saved="worldbookSaved"
        @back="pageMode = ''"
      />
      <StoryWizard
        v-else-if="pageMode === 'start'"
        :cards="cards"
        :settings="settings"
        @started="cardStarted"
        @settings="
          (channel, mode) => {
            settingsChannel = channel;
            settingsInitialPage = mode + '.model';
            panel = 'settings';
          }
        "
        @back="pageMode = ''"
      /><PersonaEditor v-else-if="pageMode === 'personas'" @back="pageMode = ''" />
      <StoryLoading
        v-else-if="storyLoad"
        :creating="storyLoad.creating"
        :name="storyLoad.name"
        :error="storyLoad.error"
        @retry="retryStoryLoad"
        @back="abandonStoryLoad"
      />
      <HomeView
        v-else-if="!session"
        :sessions="sessions"
        :portraits="sessionPortraits"
        :card-count="cards.length"
        :busy="busy"
        @new-story="startNewStory"
        @browse-cards="openSidebarSection('cards')"
        @open="(id) => guarded(() => openSession(id))"
      />

      <section v-else class="story">
        <header class="story-heading" :class="{ kemini: session.kemini }">
          <div class="story-title">
            <CharacterPortrait :src="storyPortrait" :name="name" />
            <div>
              <h1>{{ session.title || name }}</h1>
              <div class="story-meta" aria-label="角色、运行模式与渠道">
                <template v-if="session.title && session.title !== name"
                  ><span>{{ name }}</span
                  ><span class="story-meta-separator" aria-hidden="true">·</span></template
                >
                <span>{{ isFullAgent ? 'Full Agent' : 'Half Agent' }}</span>
                <span class="story-meta-separator" aria-hidden="true">·</span>
                <span class="story-channel-label">{{
                  channelLabels[session.channel_origin as Channel] || '渠道待确认'
                }}</span>
              </div>
            </div>
          </div>
          <nav class="story-view-tabs" aria-label="游玩界面">
            <button
              :class="{ active: storyView === 'dialogue' }"
              :aria-current="storyView === 'dialogue' ? 'page' : undefined"
              @click="storyView = 'dialogue'"
            >
              对话</button
            ><button
              :class="{ active: storyView === 'work' }"
              :aria-current="storyView === 'work' ? 'page' : undefined"
              :title="workCount ? `${workCount} 条工作记录` : undefined"
              @click="storyView = 'work'"
            >
              {{ isFullAgent ? 'Agent 工作记录' : '后台工作记录' }}
              <span v-if="busy" class="work-live-dot" aria-label="任务运行中"></span
              ><span v-else-if="workCount" class="tab-count" aria-hidden="true">{{ workCount }}</span>
            </button>
          </nav>
          <div class="story-tools">
            <button
              v-if="isFullAgent"
              aria-label="窗口与笔记"
              title="窗口与笔记"
              @click="panel = 'context-management'"
            >
              <Icon name="notebook" /><span class="tool-label">窗口与笔记</span>
            </button>
            <button aria-label="变量与记忆" title="变量与记忆" @click="panel = 'state'">
              <Icon name="braces" /><span class="tool-label">变量与记忆</span></button
            ><button aria-label="上下文" title="上下文预览" @click="inspectContext">
              <Icon name="layers" /><span class="tool-label">上下文</span></button
            ><button
              aria-label="剧情分支"
              title="剧情分支：继承某条回复的进度另开对话"
              :disabled="storyLocked"
              @click="showForkPoints"
            >
              <Icon name="branch" /><span class="tool-label">剧情分支</span></button
            ><button
              :aria-label="previewMessagesBusy ? '正在预览…' : '预览下次请求'"
              :disabled="storyLocked || previewMessagesBusy"
              title="导出按当前分支和输入编译的下次请求 messages；只读预览不运行神经召回"
              @click="exportNextMessages"
            >
              <Icon name="eye" /><span class="tool-label">{{
                previewMessagesBusy ? '正在预览…' : '预览下次请求'
              }}</span>
            </button>
          </div>
        </header>
        <div v-if="!session.channel_origin" class="channel-notice" role="status">
          <span>此旧对话需要确认渠道归属，确认后不可跨渠道更改。</span>
          <button @click="classifyChannel('standard')">归入普通 API</button>
          <button @click="classifyChannel('chatgpt')">归入 ChatGPT 官方</button>
        </div>
        <div v-if="contextBusy" class="channel-notice" role="status">
          <span
            >正在整理上下文 · 已处理 {{ contextOps.operation.value?.progress?.processed || 0 }} 条记录</span
          >
          <button @click="contextOps.control('cancel')">取消整理</button>
        </div>
        <div v-if="session.channel_origin && !channelModelsReady" class="channel-notice" role="status">
          <span>此渠道的模型尚未配置。</span
          ><button
            @click="
              settingsChannel = session.channel_origin;
              settingsInitialPage = session.agent_mode + '.model';
              panel = 'settings';
            "
          >
            配置{{ channelLabels[session.channel_origin as Channel] }}模型
          </button>
        </div>
        <WorkRecords
          v-if="storyView === 'work'"
          :key="session.id"
          :session-id="session.id"
          :agent-mode="session.agent_mode"
          :turn-id="activeTurn?.id"
          :events="trace"
        />
        <AppNotification
          :message="
            memoryWarning
              ? `记忆整理失败：${memoryWarning}。旧记忆保留；${session.context_strategy === 'openai_compact' ? '云端上下文仍独立保留，切回本地时会检查记忆覆盖。' : '需要裁剪时将暂停发送。'}`
              : ''
          "
        />
        <div
          v-show="storyView === 'dialogue'"
          class="dialogue-viewport"
          :style="{ '--composer-height': composerHeight + 'px' }"
        >
          <div
            ref="chat"
            class="messages"
            tabindex="-1"
            @scroll="trackScroll"
            @wheel.passive="pauseFollow"
            @touchstart.passive="readingPinned = false"
            @keydown="readingPinned = false"
          >
            <div class="chapter"><span></span> 此刻，故事由你续写 <span></span></div>
            <div v-if="writerResponseCache && !cacheBreakpointId" class="cache-breakpoint-fallback">
              <button
                class="cache-breakpoint-dot"
                :class="{ manual: !session.cache_breakpoint_auto }"
                :disabled="storyLocked || cacheBreakpointBusy || switchingVersion"
                :title="
                  session.cache_breakpoint_auto
                    ? '缓存断点自动跟进中；点击固定当前位置'
                    : '缓存断点已手动固定；点击恢复自动跟进'
                "
                :aria-label="session.cache_breakpoint_auto ? '关闭缓存断点自动跟进' : '开启缓存断点自动跟进'"
                @click="toggleCacheBreakpointFollow"
              />
              <span
                >计划断点：常驻世界书／角色设定／固定前缀 ·
                {{ session.cache_breakpoint_auto ? '自动跟进' : '已固定' }}</span
              >
            </div>
            <component
              :is="isFullAgent ? FullAgentDialogue : HalfAgentDialogue"
              ref="dialogue"
              :key="session.id"
              :messages="session.messages"
              :scroller="chat"
              :following="followLatest"
              :editing-id="editingMessageId"
              :retain-interactive="!!sessionCard?.permission"
              @reading="readingMessageId = $event"
              v-bind="
                isFullAgent
                  ? { sessionId: session.id, turn: activeTurn, events: trace, pending: !!pendingVisible }
                  : {}
              "
              @content-change="scroll()"
            >
              <template #before-message="{ message }">
                <LoreInjectionRecords
                  v-if="loreRecords.has(message.id)"
                  :records="loreRecords.get(message.id)!"
                  :removable="!pendingVisible"
                  :disabled="storyLocked || removingLore"
                  @remove="removeLore"
                />
              </template>
              <template #message="{ message }">
                <DialogueMessage
                  :message="message"
                  :interval="messageIntervals.get(message.id)"
                  :session="session"
                  :name="name"
                  :portrait-src="storyPortrait"
                  :source-highlight="sourceHighlight"
                  :writer-response-cache="writerResponseCache"
                  :cache-eligible="eligibleCacheReplies.has(message.id)"
                  :cache-breakpoint-id="cacheBreakpointId"
                  :cache-breakpoint-busy="cacheBreakpointBusy"
                  @toggle-cache-breakpoint="toggleCacheBreakpointFollow"
                  @set-cache-breakpoint="saveCacheBreakpoint(false, $event)"
                  :regenerating-id="regeneratingId"
                  :busy="busy"
                  :switching-version="switchingVersion"
                  :editable="editableMessageIds.has(message.id)"
                  :editing-message-id="editingMessageId"
                  :saving-edit="savingMessageEdit"
                  v-model:editing-message-text="editingMessageText"
                  :reviewing="activeTurn?.status === 'awaiting_review'"
                  :enabled="!!sessionCard?.permission"
                  @cancel-edit="cancelMessageEdit"
                  @save-edit="saveMessageEdit"
                  @edit="beginMessageEdit"
                  @fork="askFork"
                  @switch-version="switchReplyVersion"
                  @action="frameAction"
                />
              </template>
              <template #pending>
                <template v-if="pendingVisible">
                  <LoreInjectionRecords v-if="pendingInputLore.length" :records="pendingInputLore" />
                  <article
                    v-if="
                      visiblePendingInput && !session.messages.some((m) => m.id === visiblePendingInput?.id)
                    "
                    class="message user"
                    data-message-id="__pending__"
                  >
                    <header class="message-meta">
                      <span class="avatar">你</span><strong>{{ session.persona }}</strong>
                      <small class="message-ref">待提交输入</small>
                    </header>
                    <div class="message-body">
                      <InlineMessageEditor
                        v-if="editingMessageId === '__pending__'"
                        v-model="editingMessageText"
                        :original="visiblePendingInput.raw"
                        :busy="savingMessageEdit"
                        @cancel="cancelMessageEdit"
                        @save="savePendingEdit"
                      />
                      <template v-else>
                        <StoryProse
                          :text="visiblePendingInput.display || visiblePendingInput.raw"
                          :breaks="true"
                        />
                        <footer v-if="retryableTurn" class="message-foot">
                          <div class="message-actions">
                            <button
                              class="icon-action"
                              :disabled="busy || !!editingMessageId"
                              aria-label="编辑"
                              title="修改待提交输入并重试"
                              @click="beginPendingEdit"
                            >
                              <Icon name="pencil" :size="16" />
                            </button>
                          </div>
                        </footer>
                      </template>
                    </div>
                  </article>
                </template>
              </template>
              <template #draft>
                <article v-if="draft && pendingVisible" class="message assistant">
                  <header class="message-meta">
                    <CharacterPortrait class="avatar" :src="storyPortrait" :name="name" size="message" />
                    <strong>{{ name }}</strong
                    ><span class="badge">{{
                      updatingTracker
                        ? '后台维护中'
                        : updatingVariables
                          ? '正在更新变量'
                          : busy
                            ? isFullAgent
                              ? '正文预览 · 未保存'
                              : '正在书写'
                            : '未提交草稿'
                    }}</span>
                  </header>
                  <div class="message-body">
                    <div class="draft">
                      <StoryProse :text="draft" /><span v-if="busy" class="cursor">▍</span>
                      <ExperimentalRecords :parts="latestAuxiliary(trace)" />
                    </div>
                  </div>
                </article>
              </template>
              <template #waiting>
                <div v-if="busy && !draft" class="thinking" role="status">
                  {{
                    isFullAgent
                      ? 'Agent 正在处理当前回合…'
                      : activeTurn?.request?.scene_refresh
                        ? '正在重新整理记忆…'
                        : '正在读取设定，酝酿下一个瞬间…'
                  }}
                </div>
              </template>
            </component>
            <TurnReview
              v-if="activeTurn?.status === 'awaiting_review'"
              :changes="activeTurn.pending?.review_changes"
              @decide="review"
            />
          </div>
          <DialogueTools
            :key="session.id"
            :messages="session.messages"
            :persona="session.persona"
            :scroller="chat"
            :active="storyView === 'dialogue' && !pageMode"
            :navigate="navigateMessage"
            :release-anchor="() => dialogue?.releaseAnchor()"
            @boundary="jumpBoundary"
          />
          <ConversationRail
            :key="session.id"
            :messages="session.messages"
            :reading-id="readingMessageId"
            :breakpoint-id="writerResponseCache ? cacheBreakpointId : null"
            @navigate="navigateMessage"
          />
          <section
            v-if="!busy && (latestReply || retryableTurn) && rewriteExpanded"
            class="regeneration-panel"
            aria-label="重新生成选项"
            @keydown.esc.prevent.stop="closeRewriteOptions"
          >
            <div class="retry-toolbar regenerate-bar">
              <PromptField :prefix="activeSettings.regenerate_prompt || ''" label="重新生成补充要求">
                <input
                  v-model="regenerationNote"
                  aria-label="重新生成补充要求"
                  :placeholder="
                    retryableTurn
                      ? '补充本次要求，例如：严格遵守变量输出格式'
                      : '补充要求；将重写上一条已提交回复'
                  "
                  :disabled="busy || !!editingMessageId"
                  @keydown.enter.prevent="regenerateReply"
                />
              </PromptField>
              <button :disabled="storyLocked" @click="regenerateReply">
                <Icon name="refresh" :size="16" />重新生成
              </button>
            </div>
            <label
              v-if="canReferenceReply"
              class="regenerate-options"
              title="将原回复全文提供给模型作为修改参考，仍从本轮生成前的状态和笔记开始。"
            >
              <input v-model="regenerationIncludeReply" type="checkbox" :disabled="storyLocked" />
              带上原 AI 回复作为重写参考
            </label>
          </section>
          <div ref="composer" v-show="storyView === 'dialogue'" class="composer">
            <div class="composer-inner">
              <button
                v-if="!followLatest && !rewriteExpanded"
                class="jump-latest"
                aria-label="跳到最新"
                title="跳到最新"
                @click="scroll(true)"
              >
                <Icon name="arrow-down" :size="18" />
              </button>
              <p v-if="retryableTurn" class="composer-notice">
                {{
                  retryableTurn.request.expected_revision !== session.revision
                    ? '会话设置已变更，点击“重新生成”将按当前设置重新开始本轮。'
                    : retryableTurn.status === 'cancelled'
                      ? '本次生成已停止，草稿未提交。'
                      : '本次生成失败，正文与状态未提交。'
                }}
              </p>
              <div class="compose-box conversation-compose-frame">
                <ExpandableTextarea
                  v-model="input"
                  label="用户输入"
                  placeholder="说些什么，或描述你的行动…"
                  compact
                  expand-on-focus
                  :rows="2"
                  submit-on-enter
                  :send-shortcut="loadedSettings().shared.send_shortcut"
                  @submit="send()"
                  :disabled="busy"
                />
                <div class="compose-bottom">
                  <div class="compose-actions">
                    <button
                      ref="rewriteToggle"
                      v-if="latestReply || retryableTurn"
                      class="quiet regenerate-toggle"
                      :aria-expanded="rewriteExpanded"
                      :aria-label="rewriteExpanded ? '收起重新生成选项' : '重新生成'"
                      title="重新生成"
                      :disabled="!rewriteExpanded && (storyLocked || variablePreferenceSaving)"
                      @click="rewriteExpanded = !rewriteExpanded"
                    >
                      <Icon name="refresh" :size="16" /><span class="regenerate-label">重新生成</span></button
                    ><ConversationModelControls
                      v-if="session?.channel_origin"
                      :profile="activeModel"
                      :providers="loadedSettings().shared.providers"
                      :channel="session.channel_origin"
                      :effort="quickEffort"
                      :disabled="
                        busy ||
                        modelSaving ||
                        variablePreferenceSaving ||
                        contextBusy ||
                        activeTurn?.status === 'awaiting_review'
                      "
                      @select="selectStoryModel"
                      @update:effort="setEffort"
                    />
                  </div>
                  <span class="turn-options"
                    ><label
                      :title="
                        isFullAgent && session?.disabled_tools?.includes('patch_state')
                          ? '变量更新工具已停用，当前不会更新变量；重新启用工具后恢复此对话保存的选择。'
                          : '让模型按本轮剧情更新变量；无变化时不修改。勾选状态自动保存到当前对话，后续回合沿用；保存方式在对话设置中选择。'
                      "
                      ><input
                        v-model="updateVariables"
                        @change="saveUpdateVariables"
                        type="checkbox"
                        aria-label="本轮更新变量"
                        :disabled="
                          busy ||
                          modelSaving ||
                          variablePreferenceSaving ||
                          contextBusy ||
                          activeTurn?.status === 'awaiting_review' ||
                          (isFullAgent && session?.disabled_tools?.includes('patch_state'))
                        "
                      />
                      更新变量<span v-if="variablePreferenceSaving" class="muted" role="status">
                        · 保存中…</span
                      ></label
                    ><label
                      :title="
                        session?.agent_mode === 'full_agent'
                          ? '本轮允许修改设定：仅授权本轮修改对话内的角色设定与世界书；不会修改书架原卡'
                          : '本轮允许修改设定：先处理输入中的明确设定修改，再生成正文；正文后允许 Tracker 更新对话内角色设定与世界书'
                      "
                      ><input
                        v-model="allowEdit"
                        type="checkbox"
                        aria-label="本轮允许修改设定"
                        :disabled="busy || activeTurn?.status === 'awaiting_review'"
                      />
                      允许改设定</label
                    ></span
                  ><button v-if="busy" class="send stop" @click="stop">
                    <Icon name="stop" :size="15" />停止</button
                  ><button
                    v-else
                    class="send conversation-compose-send"
                    :disabled="
                      modelSaving ||
                      variablePreferenceSaving ||
                      contextBusy ||
                      !channelModelsReady ||
                      !session?.channel_origin ||
                      !input.trim() ||
                      activeTurn?.status === 'awaiting_review'
                    "
                    @click="send()"
                  >
                    <Icon name="arrow-up" :size="16" :stroke="1.9" />发送
                  </button>
                </div>
              </div>
              <p class="composer-meta" aria-label="游玩上下文用量">
                <ChatGPTUsage
                  :active="activeSubscription || trackerSubscription"
                  :label="storySubscriptionLabel"
                  :error="error || activeTurn?.error || undefined"
                />
                <span
                  :title="composerContextTitle"
                  :class="{ 'composer-stats-error': contextEstimateError }"
                  >{{ composerContextLabel }}</span
                >
                <span :title="conversationCacheRateTitle">缓存命中率 {{ cacheRateLabel }}</span>
              </p>
            </div>
          </div>
        </div>
        <div v-if="storyView === 'work'" class="work-view-footer">
          <span>{{
            busy
              ? '任务正在运行，工作记录会自动更新。'
              : activeTurn?.status === 'awaiting_review'
                ? '本轮更新等待审核。'
                : '工作记录已保存在当前对话中。'
          }}</span
          ><button v-if="busy" @click="stop">停止任务</button
          ><template v-if="activeTurn?.status === 'awaiting_review'"
            ><button @click="review(false)">拒绝更新</button
            ><button class="primary" @click="review(true)">接受更新</button></template
          ><button @click="storyView = 'dialogue'">返回对话</button>
        </div>
      </section>
    </main>
    <button
      v-if="rightDrawerOpen"
      class="right-sidebar-scrim"
      type="button"
      aria-label="关闭右侧栏"
      @click="closeRightSidebar"
    />
    <aside
      v-if="session && !pageMode"
      class="right-sidebar"
      :class="{
        collapsed: layout.rightCollapsed,
        'scene-active': inspectorTab === 'scene',
        'advisor-active': inspectorTab === 'advisor',
      }"
      :style="{ '--right-sidebar-width': layout.rightWidth + 'px' }"
      :inert="mobileSidebar && !layout.leftCollapsed"
      @keydown.esc="rightDrawerOpen && closeRightSidebar()"
    >
      <div
        v-if="!layout.rightCollapsed"
        class="right-sidebar-resizer"
        role="separator"
        aria-label="调整右侧栏宽度"
        aria-orientation="vertical"
        tabindex="0"
        @pointerdown="startRightResize"
        @keydown.left.prevent="resizeRightBy(20)"
        @keydown.right.prevent="resizeRightBy(-20)"
      ></div>
      <button
        v-if="rightOverlay && layout.rightCollapsed"
        ref="rightSidebarOpen"
        class="right-sidebar-open"
        title="展开右侧栏"
        aria-label="展开右侧栏"
        aria-expanded="false"
        aria-controls="inspector-panel"
        @click="toggleRightSidebar"
      >
        <Icon name="sidebar-right" :size="19" />
      </button>
      <nav v-else class="rail inspector-rail" aria-label="右侧栏">
        <div class="rail-sections" role="tablist" aria-label="当前对话">
          <button
            v-for="page in inspectorPages"
            :key="page.id"
            role="tab"
            class="rail-tab"
            :aria-selected="inspectorTab === page.id"
            :aria-expanded="inspectorTab === page.id && !layout.rightCollapsed"
            aria-controls="inspector-panel"
            :class="{ active: inspectorTab === page.id }"
            @click="openInspector(page.id)"
          >
            <Icon :name="page.icon" :size="20" /><span>{{ page.label }}</span>
          </button>
        </div>
        <div class="rail-bottom">
          <button
            ref="rightSidebarToggle"
            class="rail-button sidebar-collapse"
            :title="layout.rightCollapsed ? '展开右侧栏' : '折叠右侧栏'"
            :aria-label="layout.rightCollapsed ? '展开右侧栏' : '折叠右侧栏'"
            :aria-expanded="!layout.rightCollapsed"
            aria-controls="inspector-panel"
            @click="toggleRightSidebar"
          >
            <Icon name="sidebar-right" :size="19" />
          </button>
        </div>
      </nav>
      <div v-show="!layout.rightCollapsed" id="inspector-panel" class="inspector-panel">
        <AdvisorPanel
          v-if="advisorSeen === session.id || inspectorTab === 'advisor'"
          v-show="inspectorTab === 'advisor'"
          :key="session.id"
          :session-id="session.id"
          :story-revision="session.revision"
          :settings="loadedSettings()"
          :story-rounds="storyRounds"
          @settings="
            settingsInitialPage = 'shared.advisor';
            settingsChannel = session.channel_origin ?? undefined;
            panel = 'settings';
          "
          :story-title="session.title"
          @preference-saved="api('/sessions/' + session.id).then(applyFullSession)"
          @use-input="useAdvisorInput"
          @title="applyAdvisorTitle"
        />
        <div v-if="!layout.rightCollapsed && inspectorTab !== 'advisor'" class="inspector-content">
          <CacheBreakpointPanel
            v-if="inspectorTab === 'cache'"
            :session="session"
            :breakpoint-id="cacheBreakpointId"
            :request-state="cacheRequestState"
            :sent-label="sentCacheLabel"
            :retention="writerCacheRetention"
            :runtime="writerResponseCache"
            :disabled="storyLocked || cacheBreakpointBusy || switchingVersion"
            @save="saveCacheBreakpoint"
            @save-retention="saveCacheRetention"
            @locate="locateCacheBreakpoint"
          />
          <FullAgentTools
            v-if="isFullAgent && inspectorTab === 'tools'"
            :key="session.id"
            :session="session"
            :disabled="storyLocked"
            :update-variables="updateVariables"
            :allow-content-edit="allowEdit"
            @saved="applyFullSession"
          />
          <MvuInspector
            v-show="inspectorTab === 'mvu'"
            :session="session"
            :panel="mvuPanel"
            :scripts-enabled="!!sessionCard?.permission"
            :permission-disabled="scriptPermissionDisabled"
            @action="frameAction"
            @enable-scripts="setSessionScriptPermission(true)"
          />
          <SceneInspector
            v-show="inspectorTab === 'scene'"
            :session="session"
            :full-agent="isFullAgent"
            :locked="storyLocked"
            :lore-disabled="busy || activeTurn?.status === 'awaiting_review'"
            :context-busy="contextBusy"
            :submitting="!!sceneSubmitting"
            :updating="sceneUpdating"
            :progress="memoryProgress"
            :character="archiveCharacter"
            :saving="sceneSaving === 'memory'"
            @edit-lore="editSessionLore"
            @saved="applyFullSession"
            @refresh="refreshScene('memory')"
            @change="saveMemoryRecord"
            @cancel="stop"
            @locate="locateSource"
          />
          <SessionOptions
            v-show="inspectorTab === 'options'"
            :session="session"
            :full-agent="isFullAgent"
            :notice="optionsNotice"
            :scripts-enabled="!!sessionCard?.permission"
            :permission-disabled="scriptPermissionDisabled"
            :recipe-disabled="busy || savingRecipe || activeTurn?.status === 'awaiting_review'"
            :preference-prompt="activeSettings.preference_prompt || ''"
            :inherited-policy="activeSettings.policy"
            :presets="settings?.shared.preference_presets || []"
            @message-adapter="panel = 'message-adapter'"
            @save="saveOptions"
            @script-permission="setSessionScriptPermission"
            @presets-saved="settings = $event"
            @manage-presets="managePreferencePresets"
            @edit-recipe="panel = 'recipe'"
            @reset-recipe="resetRecipe"
          />
        </div>
      </div>
    </aside>

    <Teleport to="body"
      ><section
        v-if="deleteBubble"
        class="delete-confirm-popover"
        :class="{ above: deleteBubble.above }"
        role="alertdialog"
        aria-labelledby="delete-confirm-title"
        :style="{
          left: deleteBubble.left + 'px',
          top: deleteBubble.top + 'px',
          '--arrow-left': deleteBubble.arrowLeft + 'px',
        }"
        @keydown.esc.prevent="closeDeleteBubble()"
      >
        <div class="popover-arrow"></div>
        <h3 id="delete-confirm-title">删除“{{ deleteBubble.title }}”？</h3>
        <p>
          {{
            deleteBubble.kind === 'editor'
              ? '聊天、草稿和撤销记录将删除。已保存的角色卡、用户角色和故事保留。'
              : deleteBubble.kind === 'worldbook'
                ? '书库中的这本世界书将删除，角色卡的默认项随之解除；已挂载它的对话保留各自的副本。'
                : '剧情分支、会话记忆、检索线索和运行记录都会彻底清除，且无法恢复。'
          }}
        </p>
        <div>
          <button @click="closeDeleteBubble()">取消</button
          ><button
            class="danger"
            @click="
              deleteBubble.kind === 'editor'
                ? deleteCardConversation(deleteBubble.item)
                : deleteBubble.kind === 'worldbook'
                  ? deleteWorldbook(deleteBubble.item)
                  : deleteSession(deleteBubble.item)
            "
          >
            彻底删除
          </button>
        </div>
      </section></Teleport
    >
    <Teleport to="body">
      <div v-if="panel" class="modal-backdrop" :class="{ 'settings-backdrop': panel === 'settings' }">
        <AppModal
          :label="panel === 'settings' ? '设置' : '酒馆面板'"
          :class="{
            'context-management-modal': panel === 'context-management',
            'state-modal': panel === 'state',
            // Sized by panel, not by loaded content, so the frame keeps its height
            // while the settings chunk is still loading.
            'settings-modal': panel === 'settings',
          }"
          :size="
            ['settings', 'context', 'state', 'recipe', 'message-adapter'].includes(panel)
              ? 'large'
              : ['delete-card', 'fork', 'warmup'].includes(panel)
                ? 'small'
                : 'medium'
          "
          @close="closePanel"
          ><AppNotification :message="error" />
          <StartupWarmup
            v-if="panel === 'warmup' && startupModels"
            :status="startupModels"
            :warmup="modelWarmup"
            @close="panel = ''"
            @settings="
              settingsInitialPage = 'shared.local';
              panel = 'settings';
            "
          />
          <SettingsPanel
            v-if="panel === 'settings'"
            ref="settingsPanel"
            :initial-mode="session?.agent_mode"
            :initial-channel="settingsChannel || session?.channel_origin || undefined"
            :initial-page="settingsInitialPage"
            :warmup="modelWarmup"
            :effort-options="effortOptions"
            @updated="settings = $event"
            @imported="afterDataImport"
            @open-session="
              panel = '';
              openSession($event);
            "
            @close="panel = ''"
          />
          <DeleteCardConfirm
            v-else-if="panel === 'delete-card'"
            :name="deletingCard?.name"
            :busy="deletingCardBusy"
            @cancel="panel = ''"
            @confirm="deleteCard"
          />
          <ConversationMessageAdapter
            v-else-if="panel === 'message-adapter' && session"
            :key="session.id"
            :endpoint="`/sessions/${session.id}/message-adapter`"
            :kemini-enabled="session.kemini"
          />
          <RecipeEditor
            v-else-if="panel === 'recipe' && session"
            :recipe="session.recipe"
            :saving="savingRecipe"
            @save="saveRecipe"
          />
          <ReadingLayoutPanel v-else-if="panel === 'layout'" v-model:layout="layout" />
          <MemoryRebuildConfirm
            v-else-if="panel === 'memory-rebuild' && session"
            :estimate="memoryRebuildEstimate"
            :disabled="busy || !!sceneSubmitting"
            @start="startSceneRefresh('memory')"
            @cancel="panel = ''"
          />
          <FullAgentWindowPanel
            v-else-if="panel === 'context-management' && session"
            :session="session"
            :channel="(session.channel_origin && channelLabels[session.channel_origin]) || '渠道待确认'"
            :model="activeModel.model || '模型待配置'"
            :disabled="busy || activeTurn?.status === 'awaiting_review'"
            @saved="applyFullSession"
          />
          <StoryStatePanel
            v-else-if="panel === 'state' && session"
            v-model:tab="stateTab"
            :session="session"
            :full-agent="isFullAgent"
            :progress="memoryProgress"
            :character="archiveCharacter"
            :disabled="busy || activeTurn?.status === 'awaiting_review'"
            :saving="sceneSaving === 'memory'"
            @saved="applyFullSession"
            @change="saveMemoryRecord"
            @cancel="stop"
            @locate="locateSource"
          />
          <ForkSelectPanel
            v-else-if="panel === 'fork-select' && session"
            :points="forkPoints"
            :head="session.head"
            :name="name"
            @pick="askFork($event, true)"
            @cancel="panel = ''"
          />
          <ForkConfirmPanel
            v-else-if="panel === 'fork'"
            :name="name"
            :message="forkMessage"
            :from-picker="forkFromPicker"
            :creating="creatingFork"
            @back="panel = 'fork-select'"
            @cancel="panel = ''"
            @confirm="createFork"
          />
          <ContextPreview
            v-else-if="panel === 'context'"
            :preview="preview"
            :groups="contextGroups"
            :budget-sources="budgetSourceSummary"
            :context-tokens="Number(activeModel?.context_tokens || 0)"
            :actual-input-label="actualInputLabel"
            :length-label="contextLengthLabel"
            :tokenizer-label="tokenizerLabel"
            :cache-rate="cacheRateLabel"
            :item-title="contextItemTitle"
            :rounds="messageRounds"
          />
          <AboutPanel v-else-if="panel === 'about'" />
        </AppModal>
      </div>
    </Teleport>
  </div>
</template>
