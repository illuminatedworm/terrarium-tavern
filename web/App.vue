<script setup lang="ts">
import { notification } from './lib/notifications';
import { useTimedError } from './composables/useTimedError';
import { computed, nextTick, onMounted, onBeforeUnmount, ref, watch } from 'vue';
import { api } from './lib/api';
import LoreInjectionRecords from './components/records/LoreInjectionRecords.vue';
import { dialogueLoreRecords, type LoreRecord } from './lib/loreRecords';
import SceneNotes from './components/story/SceneNotes.vue';
import ExpandableTextarea from './components/common/ExpandableTextarea.vue';
import { highlightSource } from './lib/sceneNotes';
import StoryMessage from './components/story/StoryMessage.vue';
import StoryProse from './components/story/StoryProse.vue';
import SettingsPanel from './components/settings/SettingsPanel.vue';
import EffortControl from './components/settings/EffortControl.vue';
import PolicySelect from './components/settings/PolicySelect.vue';
import RecipeEditor from './components/settings/RecipeEditor.vue';
import CharacterEditor from './components/character/CharacterEditor.vue';
import StoryWizard from './components/story/StoryWizard.vue';
import PersonaEditor from './components/settings/PersonaEditor.vue';
import StateTree from './components/story/StateTree.vue';
import WorkRecords from './components/records/WorkRecords.vue';
import AppModal from './components/common/AppModal.vue';
import StartupWarmup from './components/story/StartupWarmup.vue';
import { observeTurn } from './lib/turnStream';
import { createSessionController, StaleView } from './lib/sessionController';
import { contextKind, groupContext } from './lib/contextPreview';
import { HISTORY_BATCH, useStoryHistory } from './lib/storyHistory';

const orphanDrafts = ref<any[]>([]);
const cards = ref<any[]>([]),
  sessions = ref<any[]>([]),
  session = ref<any>(null);
const sessionCard = ref<any>(null);
const editingSessionTitleId = ref('');
const sessionTitleDraft = ref('');
const savingSessionTitle = ref(false);
const sessionTitleInput = ref<HTMLInputElement>();
const historyView = useStoryHistory<any>(() => session.value);
const visibleMessages = historyView.visible;
const hiddenMessages = historyView.hidden;
const input = ref(''),
  error = ref(''),
  busy = ref(false),
  activeTurn = ref<any>(null),
  draft = ref('');
const panel = ref(''),
  settings = ref<any>({}),
  selected = ref<any>(null);
const preview = ref<any>(null),
  trace = ref<any[]>([]),
  savingRecipe = ref(false);
const savingSettings = ref(false);
const startupModels = ref<any>(null);
const contextStats = ref<any>(null);
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
const retryNote = ref('');
watch(
  () => activeTurn.value?.id,
  () => {
    retryNote.value = activeTurn.value?.request?.regenerate_note || '';
  },
);
const viewScope = createSessionController();
const viewApi = viewScope.request;
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
const pendingInput = ref<any>(null);
// Finished turns are reopened without replaying SSE, so recover their input
// from the persisted request. Regeneration reuses an already committed input.
const visiblePendingInput = computed(() => {
  if (pendingInput.value) return pendingInput.value;
  const request = activeTurn.value?.request;
  return request?.mode === 'send' && !request.scene_refresh && request.text ? { raw: request.text } : null;
});
const pendingLore = ref<any[]>([]);
const pendingVisible = computed(
  () =>
    activeTurn.value && activeTurn.value.status !== 'committed' && !activeTurn.value.request?.scene_refresh,
);
watch(
  () => session.value?.id,
  () => {
    pendingInput.value = null;
    pendingLore.value = [];
  },
);
const updateVariables = ref(false);
const allowEdit = ref(false),
  importInput = ref<HTMLInputElement>();
const chat = ref<HTMLElement>();
const followLatest = ref(true);
const pageMode = ref(''),
  regenerateNote = ref(''),
  quickEffort = ref('inherit');
const editingMessageId = ref(''),
  editingMessageText = ref('');
const editNotice = ref('');
const sceneSaving = ref<'memory' | null>(null);
const memoryRebuildEstimate = ref<any>(null);
const memoryProgress = computed(() => {
  if (sceneUpdating.value !== 'memory') return '';
  const batch = [...trace.value].reverse().find((e: any) => e.kind === 'memory_batch');
  return batch
    ? `正在整理第 ${batch.payload.primary_calls} 批，已提交的旧记忆保持不变。`
    : '正在准备重建记忆…';
});
const sourceHighlight = ref<{ id: string; excerpt: string; fallback: boolean } | null>(null);
const sceneSubmitting = ref<'memory' | null>(null);
const cacheBreakpointBusy = ref(false);
const writerResponseCache = computed(() => {
  const profile = settings.value?.foreground;
  const provider = settings.value?.providers?.find((item: any) => item.id === profile?.provider_id);
  return !!session.value && provider?.api_mode === 'responses' && profile?.prompt_cache_mode !== 'no_write';
});
const cacheBreakpointId = computed(() => session.value?.cache_breakpoint_effective_id || null);
const storyView = ref<'dialogue' | 'work'>('dialogue');
const sceneUpdating = computed(
  () => sceneSubmitting.value || (busy.value ? activeTurn.value?.request?.scene_refresh : null),
);
let editNoticeTimer: ReturnType<typeof setTimeout> | undefined;
function clearEditNotice() {
  clearTimeout(editNoticeTimer);
  editNoticeTimer = undefined;
  editNotice.value = '';
}
function showEditNotice() {
  clearEditNotice();
  editNotice.value =
    '修改的消息已被当前记忆覆盖。现有记忆已保留；如需按修改后的内容更新，请在右侧“场景”中重新整理。';
  editNoticeTimer = setTimeout(clearEditNotice, 5000);
}
const sidebarSection = ref(localStorage.getItem('tavern-sidebar-section') === 'cards' ? 'cards' : 'sessions');
const deleteBubble = ref<any>(null);
const inspectorTab = ref('mvu'),
  optionsNotice = ref('');
const deletingCard = ref<any>(null),
  deletingCardBusy = ref(false);
const forkMessage = ref<any>(null),
  creatingFork = ref(false);
const forkPoints = ref<any[]>([]),
  forkFromPicker = ref(false);
const regeneratingId = computed(() =>
  busy.value && activeTurn.value?.request?.mode === 'regenerate' ? activeTurn.value.request.message_id : null,
);
let deleteTrigger: HTMLElement | null = null;
watch(sidebarSection, (value) => localStorage.setItem('tavern-sidebar-section', value));
const effortOptions = [
  { value: 'inherit', label: '跟随设置' },
  { value: 'default', label: '模型默认' },
  { value: 'none', label: '关闭思考' },
  { value: 'minimal', label: '最低' },
  { value: 'low', label: '低' },
  { value: 'medium', label: '中' },
  { value: 'high', label: '高' },
  { value: 'xhigh', label: '最高' },
];
const layout = ref({
  fontSize: 18,
  lineHeight: 1.9,
  readingWidth: 1120,
  fontFamily: 'sans',
  leftCollapsed: window.innerWidth < 760,
  rightCollapsed: window.innerWidth < 760,
  rightWidth: 380,
  ...JSON.parse(localStorage.getItem('tavern-layout') || '{}'),
});
watch(layout, (value) => localStorage.setItem('tavern-layout', JSON.stringify(value)), { deep: true });
const mobileSidebarQuery = window.matchMedia('(max-width: 620px)');
const mobileSidebar = ref(mobileSidebarQuery.matches);
const rightOverlayQuery = window.matchMedia('(max-width: 760px)');
const rightOverlay = ref(rightOverlayQuery.matches);
const rightDrawerOpen = computed(
  () => rightOverlay.value && !!session.value && !pageMode.value && !layout.value.rightCollapsed,
);
const leftSidebarClose = ref<HTMLButtonElement | null>(null);
const mobileSidebarButton = ref<HTMLButtonElement | null>(null);
const rightSidebarToggle = ref<HTMLButtonElement | null>(null);
function syncMobileSidebar(event: MediaQueryListEvent) {
  mobileSidebar.value = event.matches;
}
function syncRightOverlay(event: MediaQueryListEvent) {
  rightOverlay.value = event.matches;
}
async function toggleLeftSidebar() {
  if (layout.value.leftCollapsed && mobileSidebar.value) layout.value.rightCollapsed = true;
  layout.value.leftCollapsed = !layout.value.leftCollapsed;
  if (!mobileSidebar.value) return;
  await nextTick();
  (layout.value.leftCollapsed ? mobileSidebarButton.value : leftSidebarClose.value)?.focus();
}
function toggleRightSidebar() {
  if (layout.value.rightCollapsed && rightOverlay.value) layout.value.leftCollapsed = true;
  layout.value.rightCollapsed = !layout.value.rightCollapsed;
}
async function closeRightSidebar() {
  layout.value.rightCollapsed = true;
  await nextTick();
  rightSidebarToggle.value?.focus();
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
function normalizeLayoutNumber(key: string, min: number, max: number) {
  const value = Number(layout.value[key]);
  layout.value[key] = Number.isFinite(value) ? Math.max(min, Math.min(max, value)) : min;
}
let stopRightResize: undefined | (() => void);
function resizeRightBy(delta: number) {
  layout.value.rightWidth = Math.min(640, Math.max(240, layout.value.rightWidth + delta));
}
function startRightResize(event: PointerEvent) {
  if (layout.value.rightCollapsed || window.innerWidth <= 760) return;
  event.preventDefault();
  stopRightResize?.();
  const startX = event.clientX,
    startWidth = layout.value.rightWidth;
  const move = (e: PointerEvent) => {
    layout.value.rightWidth = Math.min(640, Math.max(240, startWidth + startX - e.clientX));
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

const retryableTurn = computed(() =>
  activeTurn.value &&
  ['error', 'cancelled', 'interrupted'].includes(activeTurn.value.status) &&
  activeTurn.value.request?.expected_revision === session.value?.revision &&
  !activeTurn.value.request?.scene_refresh
    ? activeTurn.value
    : null,
);
function turnError(value: string) {
  return value?.includes('runtime.variableUpdate') || value?.includes('MVU_UPDATE_INVALID')
    ? '模型的变量更新无效，本次正文与状态未提交。草稿已保留，可以点击“重新生成”。'
    : value;
}
const latestReply = computed(() =>
  session.value?.messages.at(-1)?.role === 'assistant' && session.value?.messages.at(-1)?.parent
    ? session.value.messages.at(-1)
    : null,
);
const mvuPanel = computed(() => {
  const m = session.value?.messages
    .slice()
    .reverse()
    .find(
      (m: any) =>
        /<script/i.test(m.display || '') &&
        /stat_data|getAllVariables|getMvuData|StatusPlaceHolder/i.test(m.display || ''),
    );
  return m ? { ...m, id: session.value.head, snapshot: session.value.snapshot } : null;
});
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
  persona: '玩家',
  examples: '示例',
  memory: '记忆',
  history: '历史',
  author_note: '作者注释',
  state: '状态',
  input: '本轮输入',
  post: '后置指令',
  inject: '脚本注入',
  turn_instruction: '补充要求',
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
const pendingInputLore = computed(() =>
  pendingLore.value.filter(
    (record) => !record.manually_removed && record.anchor_id === pendingInput.value?.id,
  ),
);
const removingLore = ref(false);
async function removeLore(record: LoreRecord) {
  if (removingLore.value || busy.value || activeTurn.value?.status === 'awaiting_review') return;
  removingLore.value = true;
  try {
    await guarded(async () => {
      session.value = await viewApi(
        `/sessions/${session.value.id}/lore-injections/${encodeURIComponent(record.anchor_id)}/${encodeURIComponent(record.id)}`,
        { expected_revision: session.value.revision },
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
function contextItemTitle(item: any) {
  if (item.title) return item.title;
  const source = String(item.source || '');
  if (source.startsWith('lore:')) {
    const entry = preview.value?.lore?.find((e: any) => String(e.id) === source.slice(5));
    return entry?.comment || entry?.name || `lore · ${source.slice(5)}`;
  }
  if (source === 'persona') return `玩家 · ${session.value?.persona || ''}`;
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
const cacheRateLabel = computed(() => {
  const usage = contextStats.value?.usage;
  if (!usage) return '暂无数据';
  const prompt = usageValue(usage, ['prompt_tokens', 'input_tokens', 'promptTokenCount']);
  const cached = usageValue(usage, [
    'prompt_tokens_details.cached_tokens',
    'input_tokens_details.cached_tokens',
    'cache_read_input_tokens',
    'cached_tokens',
  ]);
  if (cached === null) return '服务商未提供';
  if (!prompt) return '0%';
  return `${Math.round((cached / prompt) * 100)}%（${cached.toLocaleString()}/${prompt.toLocaleString()} tokens）`;
});
const contextLengthLabel = computed(() =>
  contextEstimateError.value
    ? '本次 token 计数不可用'
    : contextStats.value?.estimated_tokens == null
      ? '计算中'
      : `本次预计输入 ${Number(contextStats.value.estimated_tokens).toLocaleString()} tokens`,
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
  [cards.value, sessions.value, settings.value, orphanDrafts.value] = await Promise.all([
    api('/cards'),
    api('/sessions'),
    api('/settings'),
    api('/card-edits'),
  ]);
}
async function refreshContextStats() {
  if (!session.value) return;
  const request = ++contextStatsRequest;
  const sid = session.value.id,
    token = viewScope.current();
  try {
    const [stats, records] = await Promise.allSettled([
      api(`/sessions/${sid}/preview`, { text: input.value }),
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
async function openSession(id: string) {
  const token = viewScope.enter();
  busy.value = true;
  session.value = null;
  contextStats.value = null;
  contextEstimateError.value = '';
  clearTimeout(contextRefreshTimer);
  workCount.value = 0;
  events?.close();
  events = undefined;
  sidebarSection.value = 'sessions';
  pageMode.value = '';
  inspectorTab.value = 'mvu';
  optionsNotice.value = '';
  sourceHighlight.value = null;
  clearEditNotice();
  trace.value = [];
  draft.value = '';
  editingMessageId.value = '';
  quickEffort.value = localStorage.getItem(`tavern-effort-${id}`) || 'inherit';
  const [opened, card] = await Promise.all([api(`/sessions/${id}`), api(`/sessions/${id}/card`)]).catch(
    (e) => {
      if (viewScope.owns(token)) busy.value = false;
      throw e;
    },
  );
  if (!viewScope.owns(token)) return;
  session.value = opened;
  sessionCard.value = card;
  if (window.innerWidth < 760) layout.value.leftCollapsed = true;
  const active = opened.turns.find((t: any) => ['running', 'queued', 'awaiting_review'].includes(t.status));
  activeTurn.value = active || null;
  busy.value = !!active && active.status !== 'awaiting_review';
  const last = active || opened.turns[0];
  if (
    last &&
    ['running', 'queued', 'awaiting_review', 'error', 'cancelled', 'interrupted'].includes(last.status)
  ) {
    const turn = await api(`/turns/${last.id}`);
    if (!viewScope.owns(token)) return;
    if (active || turn.request.expected_revision === opened.revision) {
      activeTurn.value = turn;
      draft.value = turn.draft;
      if (turn.status === 'error') error.value = turnError(turn.error);
      if (busy.value) observe(turn.id);
    }
  }
  localStorage.setItem('tavern-session', id);
  if (viewScope.owns(token)) await scroll(true);
  // Token estimates are auxiliary; reading must not wait for context compilation.
  if (viewScope.owns(token)) void refreshContextStats();
}

function askDeleteSession(item: any, event: MouseEvent) {
  deleteTrigger = event.currentTarget as HTMLElement;
  const rect = deleteTrigger.getBoundingClientRect(),
    width = 270,
    height = 190,
    below = rect.bottom + height + 8 < window.innerHeight,
    left = Math.max(10, Math.min(window.innerWidth - width - 10, rect.right - width));
  deleteBubble.value = {
    item,
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
function closeDeleteOnViewportChange() {
  if (deleteBubble.value) closeDeleteBubble(false);
}
async function deleteCard() {
  if (!deletingCard.value || deletingCardBusy.value) return;
  deletingCardBusy.value = true;
  try {
    await guarded(async () => {
      const id = deletingCard.value.id;
      await api(`/cards/${id}`, {}, 'DELETE');
      if (session.value?.card === id) sessionCard.value = await api(`/sessions/${session.value.id}/card`);
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
async function deleteSession(item: any) {
  closeDeleteBubble(false);
  const isCurrent = session.value?.id === item.id;
  if (isCurrent) {
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
function trackScroll() {
  const el = chat.value;
  if (el) followLatest.value = el.scrollHeight - el.scrollTop - el.clientHeight <= 48;
}
function pauseFollow(event: WheelEvent) {
  if (event.deltaY < 0 && chat.value && chat.value.scrollHeight > chat.value.clientHeight)
    followLatest.value = false;
}
function editSessionTitle(item: any) {
  editingSessionTitleId.value = item.id;
  sessionTitleDraft.value = item.title || item.name || '';
  nextTick(() => {
    sessionTitleInput.value?.focus();
    sessionTitleInput.value?.select();
  });
}
function cancelSessionTitle() {
  editingSessionTitleId.value = '';
  sessionTitleDraft.value = '';
}
async function saveSessionTitle(item: any) {
  if (savingSessionTitle.value) return;
  const title = sessionTitleDraft.value.trim();
  if (!title || title.length > 100) {
    error.value = '对话标题须为 1 到 100 个字符';
    return;
  }
  if (title === item.title) {
    cancelSessionTitle();
    return;
  }
  savingSessionTitle.value = true;
  try {
    await guarded(async () => {
      const updated = await api(
        `/sessions/${item.id}/title`,
        {
          title,
          expected_revision: session.value?.id === item.id ? session.value.revision : item.revision,
        },
        'PUT',
      );
      item.title = updated.title;
      item.revision = updated.revision;
      if (session.value?.id === item.id) {
        session.value.title = updated.title;
        session.value.revision = updated.revision;
      }
      cancelSessionTitle();
    });
  } finally {
    savingSessionTitle.value = false;
  }
}
async function showEarlierMessages() {
  const token = viewScope.current();
  followLatest.value = false;
  historyView.showEarlier();
  await nextTick();
  if (!viewScope.owns(token)) return;
  chat.value?.scrollTo({ top: 0, behavior: 'instant' });
  followLatest.value = false;
}
async function collapseHistory() {
  if (editingMessageId.value) return;
  historyView.collapse();
  await scroll(true);
}
async function scroll(force = false) {
  if (force) followLatest.value = true;
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
async function regenerateReply() {
  if (busy.value || editingMessageId.value || activeTurn.value?.status === 'awaiting_review') return;
  if (retryableTurn.value) await retryTurn();
  else if (latestReply.value) await send('regenerate', latestReply.value, '', regenerateNote.value);
}
async function retryTurn() {
  if (!retryableTurn.value || busy.value || editingMessageId.value) return;
  const request = {
    ...retryableTurn.value.request,
    update_variables: updateVariables.value,
    regenerate_note: retryNote.value.trim(),
    operation_id: crypto.randomUUID(),
    expected_revision: session.value.revision,
  };
  const token = viewScope.current();
  busy.value = true;
  try {
    activeTurn.value = await viewApi(`/sessions/${session.value.id}/turns`, request);
    workCount.value += 1;
    observe(activeTurn.value.id);
  } catch (e: any) {
    if (viewScope.owns(token)) {
      busy.value = false;
      error.value = e.message;
    }
  }
}
async function importCard(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0];
  if (!file) return;
  const form = new FormData();
  form.append('file', file);
  await guarded(async () => {
    selected.value = await api('/cards/import', form);
    await load();
    sidebarSection.value = 'cards';
    pageMode.value = 'card';
    panel.value = '';
    if (window.innerWidth < 760) layout.value.leftCollapsed = true;
  });
  (event.target as HTMLInputElement).value = '';
}
async function openOrphanDraft(id: string) {
  await guarded(async () => {
    const workspace = await api(`/card-edits/${id}`);
    showCard({ ...workspace.source, _draftOnly: true, _workspaceId: id });
  });
}
async function copyLibraryCard(card: any) {
  await guarded(async () => {
    const copied = await api(`/cards/${card.id}/copy`, {});
    await load();
    showCard(copied);
  });
}
async function cardSaved(card: any) {
  // Saving an Agent workspace, including Save As, must not remount its composer.
  selected.value =
    card?._workspaceId && selected.value
      ? { ...card, _editorKey: selected.value._editorKey || selected.value._workspaceId || selected.value.id }
      : card;
  await load();
}
function editSessionLore() {
  selected.value = {
    ...sessionCard.value,
    document: session.value.snapshot.content?.document || sessionCard.value.document,
  };
  pageMode.value = 'session-lore';
}
function sessionLoreSaved(value: any) {
  session.value = value;
  activeTurn.value = value.turns.find((turn: any) => turn.status === 'awaiting_review') || null;
  pageMode.value = '';
}
function showCard(card: any) {
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
function manageCards() {
  viewScope.enter();
  events?.close();
  sidebarSection.value = 'cards';
  session.value = null;
  pageMode.value = '';
}
async function cardStarted(s: any) {
  sidebarSection.value = 'sessions';
  await load();
  await openSession(s.id);
}
function setEffort(value: string) {
  quickEffort.value = value;
  if (session.value) localStorage.setItem(`tavern-effort-${session.value.id}`, value);
}
async function send(mode = 'send', message?: any, text?: string, note = '') {
  if (!session.value || busy.value || sceneSubmitting.value) return;
  const value = text ?? input.value;
  if (mode === 'send' && !value.trim()) return;
  const sid = session.value.id,
    token = viewScope.current();
  busy.value = true;
  error.value = '';
  pendingInput.value = null;
  pendingLore.value = [];
  try {
    const turn = await api(`/sessions/${sid}/turns`, {
      text: value,
      mode,
      message_id: message?.id,
      operation_id: crypto.randomUUID(),
      expected_revision: session.value.revision,
      allow_content_edit: allowEdit.value,
      update_variables: updateVariables.value,
      reasoning_effort: quickEffort.value === 'inherit' ? null : quickEffort.value,
      regenerate_note: note,
    });
    if (!viewScope.owns(token)) return;
    activeTurn.value = turn;
    if (mode === 'send') pendingInput.value = { raw: value };
    if (mode === 'send' && input.value === value) input.value = '';
    regenerateNote.value = '';
    rewriteExpanded.value = false;
    workCount.value += 1;
    observe(turn.id);
  } catch (e: any) {
    if (viewScope.owns(token)) {
      error.value = e.message;
      busy.value = false;
    }
  }
}

function observe(id: string) {
  events?.close();
  const sid = session.value.id,
    token = viewScope.current();
  // Rebuild from sequence zero on reopen instead of appending replay to a draft snapshot.
  draft.value = '';
  trace.value = [];
  events = observeTurn(
    id,
    () => viewScope.owns(token),
    (event) => {
      if (event.kind === 'text') {
        draft.value += event.payload.text;
        void scroll();
      } else {
        trace.value.push(event);
        if (event.kind === 'input_ready') {
          pendingInput.value = event.payload.message;
          pendingLore.value = event.payload.lore_injections || [];
          void scroll();
        }
        if (event.kind === 'scene_refresh_ready' && event.payload.purpose === 'memory') {
          delete session.value.snapshot.memory_warning;
          memoryWarning.value = '';
          error.value = '';
        }
        if (event.kind === 'error') error.value = turnError(event.payload.message);
        if (event.kind === 'memory_failed') session.value.snapshot.memory_warning = event.payload.message;
        if (event.kind === 'memory_ready') delete session.value.snapshot.memory_warning;
        if (event.kind === 'memory_records_discarded') memoryDiscardNotice.value = event.payload.message;
      }
    },
    async () => {
      try {
        const [turn, updated] = await Promise.all([api(`/turns/${id}`), api(`/sessions/${sid}`)]);
        if (!viewScope.owns(token)) return;
        const following = followLatest.value,
          top = chat.value?.scrollTop || 0;
        activeTurn.value = turn;
        draft.value = turn.draft;
        busy.value = false;
        session.value = updated;
        await nextTick();
        if (!viewScope.owns(token)) return;
        if (following) await scroll();
        else {
          chat.value?.scrollTo({ top, behavior: 'instant' });
          followLatest.value = false;
        }
        await load();
        await refreshContextStats();
      } catch (e: any) {
        if (viewScope.owns(token)) {
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
        `/sessions/${session.value.id}/memory/records`,
        { ...change, expected_revision: session.value.revision },
        'PUT',
      );
      await refreshContextStats();
    });
  } finally {
    sceneSaving.value = null;
  }
}
async function locateSource(id: string, excerpt: string) {
  const message = session.value?.messages.find((m: any) => m.id === id);
  if (!message) return;
  panel.value = '';
  storyView.value = 'dialogue';
  followLatest.value = false;
  sourceHighlight.value = { id, excerpt, fallback: false };
  historyView.reveal(id);
  await nextTick();
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
      memoryRebuildEstimate.value = await viewApi(`/sessions/${session.value.id}/memory/rebuild-estimate`);
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
      activeTurn.value = await viewApi(`/sessions/${session.value.id}/scene/${kind}/regenerate`, {
        operation_id: crypto.randomUUID(),
        expected_revision: session.value.revision,
      });
      panel.value = '';
      busy.value = true;
      draft.value = '';
      trace.value = [];
      observe(activeTurn.value.id);
    });
  } finally {
    sceneSubmitting.value = null;
  }
}
async function stop() {
  if (activeTurn.value) await guarded(() => api(`/turns/${activeTurn.value.id}/cancel`, {}));
}
async function review(accept: boolean) {
  const sid = session.value.id,
    token = viewScope.current();
  await guarded(async () => {
    await api(`/turns/${activeTurn.value.id}/review`, { accept });
    if (viewScope.owns(token)) await openSession(sid);
  });
}
function beginPendingEdit() {
  editingMessageId.value = '__pending__';
  editingMessageText.value = visiblePendingInput.value.raw;
}
async function savePendingEdit() {
  if (!editingMessageText.value.trim() || !retryableTurn.value) return;
  const text = editingMessageText.value;
  await guarded(async () => {
    activeTurn.value = await viewApi(
      `/turns/${activeTurn.value.id}/input`,
      { text, expected_revision: session.value.revision },
      'PUT',
    );
    pendingInput.value = { raw: text };
    pendingLore.value = [];
    draft.value = '';
    cancelMessageEdit();
  });
}
function beginMessageEdit(message: any) {
  editingMessageId.value = message.id;
  editingMessageText.value = message.raw;
}
function cancelMessageEdit() {
  editingMessageId.value = '';
  editingMessageText.value = '';
}
const switchingVersion = ref(false);
async function switchReplyVersion(message: any, offset: number) {
  if (
    busy.value ||
    switchingVersion.value ||
    editingMessageId.value ||
    activeTurn.value?.status === 'awaiting_review'
  )
    return;
  const target = message.versions?.[message.version_index + offset];
  if (!target || message.id !== session.value?.head) return;
  switchingVersion.value = true;
  try {
    await guarded(async () => {
      session.value = await viewApi(
        `/sessions/${session.value.id}/messages/${message.id}/version`,
        { head: target, expected_revision: session.value.revision },
        'PUT',
      );
      await refreshContextStats();
    });
  } finally {
    switchingVersion.value = false;
  }
}
async function saveMessageEdit(message: any) {
  if (!editingMessageText.value.trim()) return;
  await guarded(async () => {
    session.value = await viewApi(
      `/sessions/${session.value.id}/messages/${message.id}`,
      { text: editingMessageText.value, expected_revision: session.value.revision },
      'PUT',
    );
    cancelMessageEdit();
    if (session.value.edit_affected_memory) showEditNotice();
    await load();
    await refreshContextStats();
  });
}
async function inspectContext() {
  await guarded(async () => {
    preview.value = await viewApi(`/sessions/${session.value.id}/preview`, { text: input.value });
    contextStats.value = preview.value;
    panel.value = 'context';
  });
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
    forkPoints.value = (await viewApi(`/sessions/${session.value.id}/branches`)).filter(
      (message: any) => message.role === 'assistant',
    );
    panel.value = 'fork-select';
  });
}
function askFork(message: any, fromPicker = false) {
  if (!message || busy.value || editingMessageId.value || activeTurn.value?.status === 'awaiting_review')
    return;
  forkMessage.value = message;
  forkFromPicker.value = fromPicker;
  panel.value = 'fork';
}
async function createFork() {
  if (!session.value || !forkMessage.value || creatingFork.value) return;
  creatingFork.value = true;
  try {
    await guarded(async () => {
      const originalId = session.value.id,
        effort = quickEffort.value;
      const created = await api(`/sessions/${originalId}/fork`, {
        message_id: forkMessage.value.id,
        expected_revision: session.value.revision,
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
async function saveSettings() {
  if (savingSettings.value) return;
  savingSettings.value = true;
  try {
    await guarded(async () => {
      settings.value = await api('/settings', settings.value, 'PUT');
      panel.value = '';
    });
  } finally {
    savingSettings.value = false;
  }
}
async function probe() {
  await guarded(async () => {
    preview.value = await api('/settings/probe', {});
    settings.value = await api('/settings');
    panel.value = 'probe';
  });
}
async function saveOptions() {
  await guarded(async () => {
    session.value = await viewApi(
      `/sessions/${session.value.id}/options`,
      {
        persona: session.value.persona,
        persona_data: session.value.persona_data,
        policy: session.value.policy,
        turn_preference: session.value.turn_preference || '',
        expected_revision: session.value.revision,
      },
      'PUT',
async function saveCacheBreakpoint(automatic: boolean, messageId: string | null = null) {
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
      const sid = session.value.id;
      const updated = await viewApi(
        `/sessions/${sid}/cache-breakpoint`,
        { automatic, message_id: automatic ? null : messageId, expected_revision: session.value.revision },
        'PUT',
      );
      for (const field of [
        'revision',
        'cache_breakpoint_auto',
        'cache_breakpoint_message',
        'cache_breakpoint_effective_id',
      ])
        session.value[field] = updated[field];
      const listed = sessions.value.find((item) => item.id === sid);
      if (listed) listed.revision = updated.revision;
    });
  } finally {
    cacheBreakpointBusy.value = false;
  }
}
function toggleCacheBreakpointFollow() {
  const automatic = !session.value?.cache_breakpoint_auto;
  void saveCacheBreakpoint(automatic, automatic ? null : cacheBreakpointId.value);
}
    );
    optionsNotice.value = '已保存';
  });
}
async function saveRecipe(recipe: any) {
  if (savingRecipe.value) return;
  savingRecipe.value = true;
  try {
    await guarded(async () => {
      session.value = await viewApi(
        `/sessions/${session.value.id}/recipe`,
        { recipe, expected_revision: session.value.revision },
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
        `/sessions/${session.value.id}/recipe`,
        { use_global: true, expected_revision: session.value.revision },
        'PUT',
      );
      optionsNotice.value = '已采用当前全局编排';
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
      const turn = await api(`/sessions/${session.value.id}/turns`, {
        text: args.user_input || '继续',
        generate: args,
        expected_revision: session.value.revision,
        operation_id: crypto.randomUUID(),
        reasoning_effort: quickEffort.value === 'inherit' ? null : quickEffort.value,
      });
      activeTurn.value = turn;
      busy.value = true;
      draft.value = '';
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
    const updated = await viewApi(`/sessions/${session.value.id}/interaction`, {
      method,
      args,
      expected_revision: session.value.revision,
      operation_id: crypto.randomUUID(),
      allow_content_edit: allowEdit.value,
    });
    session.value = updated;
    const review = updated.turns.find((t: any) => t.status === 'awaiting_review');
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
    if (sessions.value.some((s) => s.id === last)) await openSession(last!);
    const status = await api('/local-models');
    const enabled = [
      ...(settings.value.retrieval?.channels?.includes('embedding') ? ['embedding'] : []),
      ...(settings.value.local_models?.reranker_enabled ? ['reranker'] : []),
    ].filter(
      (purpose) =>
        !status.models[purpose].ready &&
        !status.jobs.some(
          (job: any) => job.kind === `warmup:${purpose}` && ['queued', 'running'].includes(job.status),
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
  clearEditNotice();
  clearTimeout(contextRefreshTimer);
  document.removeEventListener('pointerdown', closeDeleteOnOutside);
  document.removeEventListener('scroll', closeDeleteOnViewportChange, true);
  window.removeEventListener('resize', closeDeleteOnViewportChange);
  events?.close();
  stopRightResize?.();
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
      <button
        ref="leftSidebarClose"
        class="sidebar-collapse"
        @click="toggleLeftSidebar"
        :aria-label="layout.leftCollapsed ? '展开左侧栏' : '折叠左侧栏'"
        :aria-expanded="!layout.leftCollapsed"
        aria-controls="primary-sidebar"
      >
        {{ layout.leftCollapsed ? '›' : '‹' }}
      </button>
      <a class="brand" href="#" @click.prevent="startNewStory"
        ><span class="brand-mark">♧</span><span>Terrarium<small>T A V E R N</small></span></a
      >
      <div class="sidebar-sections" role="tablist" aria-label="酒馆管理">
        <button
          role="tab"
          :aria-selected="sidebarSection === 'sessions'"
          :class="{ active: sidebarSection === 'sessions' }"
          @click="sidebarSection = 'sessions'"
        >
          会话</button
        ><button
          role="tab"
          :aria-selected="sidebarSection === 'cards'"
          :class="{ active: sidebarSection === 'cards' }"
          @click="manageCards"
        >
          角色卡
        </button>
      </div>
      <template v-if="sidebarSection === 'sessions'"
        ><button class="new-story" :disabled="busy" @click="startNewStory">＋ 开始新故事</button>
        <div class="side-label">
          会话管理 <span>{{ sessions.length }}</span>
        </div>
        <nav class="session-list">
          <div
            v-for="s in sessions"
            :key="s.id"
            class="session-row"
            :class="{ active: session?.id === s.id }"
          >
            <div v-if="editingSessionTitleId === s.id" class="session-title-editor">
              <input
                ref="sessionTitleInput"
                v-model="sessionTitleDraft"
                maxlength="100"
                aria-label="对话标题"
                @keydown.enter="saveSessionTitle(s)"
                @keydown.esc="cancelSessionTitle"
              />
              <button :disabled="savingSessionTitle" @click="saveSessionTitle(s)">保存</button>
              <button :disabled="savingSessionTitle" @click="cancelSessionTitle">取消</button>
            </div>
            <template v-else
              ><button class="session-open" @click="guarded(() => openSession(s.id))">
                <span class="session-icon">◇</span
                ><span
                  >{{ s.title || s.name
                  }}<small
                    >{{ new Date(s.created * 1000).toLocaleDateString('zh-CN') }} · {{ s.persona }}</small
                  ></span
                ></button
              ><button
                class="session-rename"
                :aria-label="`重命名对话 ${s.title || s.name}`"
                @click.stop="editSessionTitle(s)"
              >
                改名</button
              ><button
                class="session-delete"
                :aria-label="`删除会话 ${s.title || s.name}`"
                @click.stop="askDeleteSession(s, $event)"
              >
                删除
              </button></template
            >
          </div>
          <p v-if="!sessions.length" class="empty-small">故事开始后，会留在这里。</p>
        </nav></template
      >
      <template v-else
        ><button class="new-story import-only" @click="importInput?.click()">↓ 导入角色卡</button
        ><input ref="importInput" type="file" accept=".json,.png,.charx" hidden @change="importCard" />
        <div class="side-label">
          角色卡管理 <span>{{ cards.length }}</span>
        </div>
        <nav class="session-list card-side-list">
          <div v-for="card in cards" :key="card.id" class="card-side-row" :data-card-id="card.id">
            <button
              class="card-side-item"
              :class="{ active: selected?.id === card.id && pageMode === 'card' }"
              @click="showCard(card)"
            >
              <span class="session-icon">◇</span
              ><span
                >{{ card.name
                }}<small>{{ card.report.variables === 'mvu_detected' ? 'MVU' : '普通角色卡' }}</small></span
              >
            </button>
            <div class="card-row-actions" role="group" :aria-label="`${card.name} 操作`">
              <button
                class="card-copy-action"
                :aria-label="`复制角色卡 ${card.name}`"
                @click="copyLibraryCard(card)"
              >
                复制</button
              ><button
                class="card-delete-action"
                :aria-label="`删除角色卡 ${card.name}`"
                @click="
                  deletingCard = card;
                  panel = 'delete-card';
                "
              >
                删除
              </button>
            </div>
          </div>
          <p v-if="!cards.length" class="empty-small">从上方导入 JSON、PNG 或 CHARX 角色卡。</p>
          <p v-if="orphanDrafts.length" class="side-label">原卡已删除的编辑草稿</p>
          <button v-for="draft in orphanDrafts" :key="draft.id" @click="openOrphanDraft(draft.id)">
            继续编辑 {{ draft.name }}
          </button>
        </nav></template
      >
      <div class="sidebar-bottom">
        <button @click="pageMode = 'personas'">♙ 玩家角色</button
        ><button @click="panel = 'layout'">Aa 阅读排版</button
        ><button @click="panel = 'settings'">⚙ 设置</button
        ><button @click="panel = 'about'">关于 Terrarium Tavern ↗</button>
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
        ☰
      </button>
      <AppNotification :message="error" />
      <AppNotification :message="editNotice" />
      <AppNotification :message="memoryDiscardNotice" />

      <CharacterEditor
        v-if="pageMode === 'card' && selected"
        :key="selected._editorKey || selected._workspaceId || selected.id"
        :card="selected"
        @saved="cardSaved"
        @workspace-selected="
          (id) => {
            selected = {
              ...selected,
              _editorKey: selected._editorKey || selected._workspaceId || selected.id,
              _workspaceId: id,
            };
          }
        "
        @back="pageMode = ''"
      /><CharacterEditor
        v-else-if="pageMode === 'session-lore' && selected && session"
        :key="`lore-${session.id}`"
        :card="selected"
        :session="session"
        @session-saved="sessionLoreSaved"
        @back="pageMode = ''"
      />
      <StoryWizard
        v-else-if="pageMode === 'start'"
        :cards="cards"
        @started="cardStarted"
        @back="pageMode = ''"
      /><PersonaEditor v-else-if="pageMode === 'personas'" @back="pageMode = ''" />
      <section v-else-if="!session" class="library">
        <div class="eyebrow">A PLACE FOR YOUR STORIES</div>
        <h1>推开门，<br />进入另一个世界。</h1>
        <p class="intro">让角色拥有记忆，让每一个选择留下回响。<br />从一张角色卡，开始属于你的故事。</p>
        <div class="library-heading">
          <h2>开始新的故事</h2>
          <span>{{ cards.length }} 张角色卡</span>
        </div>
        <div class="library-guide">
          <span>◇</span>
          <div>
            <strong>{{ cards.length ? '从左侧选择一张角色卡' : '先从左侧导入角色卡' }}</strong>
            <p>
              {{
                cards.length
                  ? '角色卡可在左侧浏览和编辑。创建故事请到“会话”页点击“开始新故事”，再选择角色卡。'
                  : '角色卡仅能从左侧“角色卡”分类导入。支持 JSON、PNG 与 CHARX。'
              }}
            </p>
          </div>
        </div>
        <div class="library-note">
          <span>✦</span>
          <p>每条剧情都可以分支。尝试不同选择，原来的故事依然在。</p>
        </div>
      </section>

      <section v-else class="story">
        <div class="story-heading">
          <h1>{{ name }}</h1>
          <div class="story-tools">
            <button @click="panel = 'state'">状态与记忆</button
            ><button
              :disabled="busy || !!editingMessageId || activeTurn?.status === 'awaiting_review'"
              @click="showForkPoints"
            >
              剧情分支
            </button>
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
            @click="storyView = 'work'"
          >
            后台工作记录 <span v-if="busy" class="work-live-dot" aria-label="任务运行中"></span>
          </button>
        </nav>
        <WorkRecords
          v-if="storyView === 'work'"
          :key="session.id"
          :session-id="session.id"
          :turn-id="activeTurn?.id"
          :events="trace"
        />
        <div
          v-show="storyView === 'dialogue'"
          ref="chat"
          class="messages"
          @scroll="trackScroll"
          @wheel.passive="pauseFollow"
        >
          <div class="chapter"><span></span> 此刻，故事由你续写 <span></span></div>
          <nav
            v-if="session.messages.length > HISTORY_BATCH"
            class="history-controls"
            aria-label="历史消息显示"
          >
            <span>显示 {{ visibleMessages.length }} / {{ session.messages.length }} 条消息</span>
            <button v-if="hiddenMessages" @click="showEarlierMessages">
              显示更早消息（{{ Math.min(HISTORY_BATCH, hiddenMessages) }} 条）
            </button>
            <button
              v-if="visibleMessages.length > HISTORY_BATCH"
              :disabled="!!editingMessageId"
              @click="collapseHistory"
            >
              收起到最近 {{ HISTORY_BATCH }} 条
            </button>
          </nav>
          <template v-for="message in visibleMessages" :key="message.id"
            ><LoreInjectionRecords
              v-if="loreRecords.has(message.id)"
              :records="loreRecords.get(message.id)!"
              :removable="!pendingVisible"
              :disabled="
                busy || removingLore || !!editingMessageId || activeTurn?.status === 'awaiting_review'
              "
              @remove="removeLore" />
            <article
              :data-message-id="message.id"
              class="message"
              :class="[
                message.role,
                {
                  regenerating: message.id === regeneratingId,
                  'source-message-highlight': sourceHighlight?.id === message.id,
                },
          <div v-if="writerResponseCache && !cacheBreakpointId" class="cache-breakpoint-fallback">
            <button
              class="cache-breakpoint-dot"
              :class="{ manual: !session.cache_breakpoint_auto }"
              :disabled="
                busy || cacheBreakpointBusy || activeTurn?.status === 'awaiting_review' || switchingVersion
              "
              :title="
                session.cache_breakpoint_auto
                  ? '缓存断点自动跟进中；点击固定当前位置'
                  : '缓存断点已手动固定；点击恢复自动跟进'
              "
              :aria-label="session.cache_breakpoint_auto ? '关闭缓存断点自动跟进' : '开启缓存断点自动跟进'"
              @click="toggleCacheBreakpointFollow"
            />
            <span
              >缓存断点：常驻世界书末尾（无常驻条目时为角色设定） ·
              {{ session.cache_breakpoint_auto ? '自动跟进' : '已固定' }}</span
            >
          </div>
              ]"
              :aria-busy="message.id === regeneratingId"
            >
              <div class="avatar">{{ message.role === 'user' ? '你' : name.slice(0, 1) }}</div>
              <div class="message-body">
                <div class="message-meta">
                  <strong>{{ message.role === 'user' ? session.persona : name }}</strong>
                </div>
                <div v-if="editingMessageId === message.id" class="inline-message-editor">
            <button
              v-if="
                writerResponseCache &&
                cacheBreakpointId &&
                !visibleMessages.some((item: any) => item.id === cacheBreakpointId)
              "
              @click="historyView.reveal(cacheBreakpointId)"
            >
              显示缓存断点
            </button>
                  <textarea
                    v-model="editingMessageText"
                    rows="5"
                    autofocus
                    @keydown.escape.prevent="cancelMessageEdit"
                    @keydown.ctrl.enter.prevent="saveMessageEdit(message)"
                  />
                  <div>
                    <span>Ctrl + Enter 保存 · Esc 取消</span><button @click="cancelMessageEdit">取消</button
                    ><button
                      class="primary"
                      :disabled="!editingMessageText.trim()"
                      @click="saveMessageEdit(message)"
                    >
                      保存修改
                    </button>
                  </div>
                </div>
                <StoryMessage
                  v-else
                  :hide-panel="!!mvuPanel"
                  :message="message"
                  :session="session"
                  :enabled="!!currentCard?.permission"
                  @action="frameAction"
                />
                <details v-if="message.retrieval_query" class="retrieval-clue">
                  <summary>检索线索</summary>
                  <p>{{ message.retrieval_query }}</p>
              <button
                v-if="writerResponseCache && cacheBreakpointId === message.id"
                class="cache-breakpoint-dot"
                :class="{ manual: !session.cache_breakpoint_auto }"
                :disabled="
                  busy || cacheBreakpointBusy || activeTurn?.status === 'awaiting_review' || switchingVersion
                "
                :title="
                  session.cache_breakpoint_auto
                    ? '缓存断点自动跟进中；点击固定在此回复末尾'
                    : '缓存断点已固定；点击恢复自动跟进'
                "
                :aria-label="session.cache_breakpoint_auto ? '关闭缓存断点自动跟进' : '开启缓存断点自动跟进'"
                @click="toggleCacheBreakpointFollow"
              />
                </details>
                <div
                  v-if="sourceHighlight?.id === message.id && sourceHighlight?.fallback"
                  class="source-excerpt"
                  <small
                    v-if="writerResponseCache && cacheBreakpointId === message.id"
                    class="cache-breakpoint-status"
                    >{{ session.cache_breakpoint_auto ? '缓存自动跟进' : '缓存位置已固定' }}</small
                  >
                >
                  <small>引用原文</small><mark>{{ sourceHighlight?.excerpt }}</mark>
                </div>
                <div v-if="editingMessageId !== message.id" class="message-actions">
                  <template v-if="message.role === 'assistant' && message.id === session.head">
                    <button
                      class="icon-action"
                      :disabled="
                        busy ||
                        switchingVersion ||
                        !!editingMessageId ||
                        activeTurn?.status === 'awaiting_review' ||
                        !message.version_index
                      "
                      aria-label="上一个版本"
                      title="上一个版本（更旧）"
                      @click="switchReplyVersion(message, -1)"
                    >
                      <svg width="14" height="14" viewBox="0 0 20 20" fill="none" aria-hidden="true">
                        <path
                          d="m12 4-6 6 6 6"
                          stroke="currentColor"
                          stroke-width="1.4"
                          stroke-linecap="round"
                          stroke-linejoin="round"
                        />
                      </svg>
                    </button>
                    <span class="muted" aria-live="polite"
                      >{{ (message.version_index ?? 0) + 1 }} / {{ message.versions?.length || 1 }}</span
                    >
                    <button
                      class="icon-action"
                      :disabled="
                        busy ||
                        switchingVersion ||
                        !!editingMessageId ||
                        activeTurn?.status === 'awaiting_review' ||
                        (message.version_index ?? 0) >= (message.versions?.length || 1) - 1
                      "
                      aria-label="下一个版本"
                      title="下一个版本（更新）"
                      @click="switchReplyVersion(message, 1)"
                    >
                      <svg width="14" height="14" viewBox="0 0 20 20" fill="none" aria-hidden="true">
                        <path
                          d="m8 4 6 6-6 6"
                          stroke="currentColor"
                          stroke-width="1.4"
                          stroke-linecap="round"
                          stroke-linejoin="round"
                        />
                      </svg>
                    </button>
                  </template>
                  <button
                    :disabled="busy || !!editingMessageId"
                    :class="{ 'icon-action': message.role === 'assistant' }"
                    aria-label="编辑"
                    title="编辑回复"
                    @click="beginMessageEdit(message)"
                  >
                    <svg
                      v-if="message.role === 'assistant'"
                      width="14"
                      height="14"
                      viewBox="0 0 20 20"
                      fill="none"
                      aria-hidden="true"
                    >
                      <path
                        d="m12.5 3.5 4 4M3 17l1-4L14 3a2.1 2.1 0 0 1 3 3L7 16l-4 1Z"
                        stroke="currentColor"
                        stroke-width="1.4"
                        stroke-linecap="round"
                        stroke-linejoin="round"
                      /></svg
                    ><template v-else>编辑</template></button
                  ><button
                    v-if="message.role === 'assistant'"
                    class="icon-action"
                    :disabled="busy || !!editingMessageId || activeTurn?.status === 'awaiting_review'"
                    aria-label="分支"
                    title="继承此处进度，新建对话"
                    @click="askFork(message)"
                  >
                    <svg width="14" height="14" viewBox="0 0 20 20" fill="none" aria-hidden="true">
                      <circle cx="5" cy="4" r="2" stroke="currentColor" stroke-width="1.4" />
                      <circle cx="5" cy="16" r="2" stroke="currentColor" stroke-width="1.4" />
                      <circle cx="15" cy="4" r="2" stroke="currentColor" stroke-width="1.4" />
                      <path
                        d="M5 6v8m0-4h5a5 5 0 0 0 5-4"
                        stroke="currentColor"
                        stroke-width="1.4"
                        stroke-linecap="round"
                      />
                    </svg>
                  </button>
                </div>
              </div></article
          ></template>
          <template v-if="pendingVisible">
            <LoreInjectionRecords v-if="pendingInputLore.length" :records="pendingInputLore" />
            <article
              v-if="
                visiblePendingInput && !session.messages.some((m: any) => m.id === visiblePendingInput.id)
              "
              class="message user"
            >
              <div class="avatar">你</div>
              <div class="message-body">
                <div class="message-meta">
                  <strong>{{ session.persona }}</strong>
                </div>
                <div v-if="editingMessageId === '__pending__'" class="inline-message-editor">
                  <textarea
                    v-model="editingMessageText"
                    aria-label="编辑最新用户消息"
                    @keydown.ctrl.enter.prevent="savePendingEdit"
                    @keydown.escape.prevent="cancelMessageEdit"
                  />
                  <div class="actions">
                    <button @click="cancelMessageEdit">取消</button
                    ><button :disabled="!editingMessageText.trim()" @click="savePendingEdit">保存</button>
                  </div>
                </div>
                <template v-else>
                  <StoryProse :text="visiblePendingInput.display || visiblePendingInput.raw" :breaks="true" />
                  <div v-if="retryableTurn" class="message-actions">
                    <button :disabled="busy || !!editingMessageId" @click="beginPendingEdit">编辑</button>
                  </div>
                </template>
              </div>
                  <button
                    v-if="message.role === 'assistant' && writerResponseCache"
                    class="icon-action cache-set-action"
                    :class="{ active: !session.cache_breakpoint_auto && cacheBreakpointId === message.id }"
                    :disabled="
                      busy ||
                      cacheBreakpointBusy ||
                      !!editingMessageId ||
                      activeTurn?.status === 'awaiting_review'
                    "
                    :aria-label="
                      cacheBreakpointId === message.id && !session.cache_breakpoint_auto
                        ? '缓存断点已固定在此回复末尾'
                        : '将缓存断点固定在此回复末尾'
                    "
                    :title="
                      cacheBreakpointId === message.id && !session.cache_breakpoint_auto
                        ? '缓存断点已固定在此回复末尾'
                        : '将缓存断点固定在此回复末尾'
                    "
                    @click="saveCacheBreakpoint(false, message.id)"
                  >
                    <svg width="16" height="16" viewBox="0 0 20 20" fill="none" aria-hidden="true">
                      <path
                        d="M4 4v12m0-2h11l-2.5-3L15 8H4"
                        stroke="currentColor"
                        stroke-width="1.5"
                        stroke-linecap="round"
                        stroke-linejoin="round"
                      />
                      <circle cx="4" cy="4" r="2" fill="currentColor" />
                    </svg>
                  </button>
            </article>
          </template>
          <article v-if="draft && activeTurn?.status !== 'committed'" class="message assistant">
            <div class="avatar">{{ name.slice(0, 1) }}</div>
            <div class="message-body">
              <div class="message-meta">
                <strong>{{ name }}</strong
                ><span class="badge">{{
                  updatingVariables ? '正在更新变量' : busy ? '正在书写' : '未提交草稿'
                }}</span>
              </div>
              <div class="draft"><StoryProse :text="draft" /><span v-if="busy" class="cursor">▍</span></div>
            </div>
          </article>
          <div v-if="busy && !draft" class="thinking">
            ✦ {{ activeTurn?.request?.scene_refresh ? '正在重新整理记忆…' : '正在读取设定，酝酿下一个瞬间…' }}
          </div>
          <div v-if="activeTurn?.status === 'awaiting_review'" class="review">
            <strong>本轮状态或设定有修改，等待你的决定</strong>
            <details>
              <summary>查看修改</summary>
              <pre>{{ JSON.stringify(activeTurn.pending, null, 2) }}</pre>
            </details>
            <button class="primary" @click="review(true)">接受并继续</button
            ><button @click="review(false)">保留草稿，取消修改</button>
          </div>
        </div>
        <AppNotification
          :message="memoryWarning ? `记忆整理失败：${memoryWarning}。旧记忆保留；需要裁剪时将暂停发送。` : ''"
        />
        <div v-show="storyView === 'dialogue'" class="composer">
          <div class="composer-inner">
            <button
              v-if="!followLatest"
              class="jump-latest"
              aria-label="跳到最新"
              title="跳到最新"
              @click="scroll(true)"
            >
              ↓
            </button>
            <AppNotification
              v-if="retryableTurn"
              :message="
                activeTurn.status === 'cancelled'
                  ? '本次生成已停止，草稿未提交。'
                  : '本次生成失败，正文与状态未提交。'
              "
            />
            <div
              class="retry-toolbar regenerate-bar"
              v-if="(latestReply || retryableTurn) && rewriteExpanded"
            >
              <input
                v-model="regenerationNote"
                aria-label="重新生成补充要求"
                maxlength="500"
                :placeholder="
                  retryableTurn
                    ? '补充本次要求，例如：严格遵守变量输出格式'
                    : '补充要求；将重写上一条已提交回复'
                "
                :disabled="busy || !!editingMessageId"
                @keydown.enter.prevent="regenerateReply"
              /><button
                :disabled="busy || !!editingMessageId || activeTurn?.status === 'awaiting_review'"
                @click="regenerateReply"
              >
                ↻ 重新生成
              </button>
            </div>
            <div class="compose-box">
              <textarea
                v-model="input"
                placeholder="说些什么，或描述你的行动…"
                @keydown.enter.exact.prevent="send()"
                :disabled="busy"
                rows="2"
              />
              <div class="compose-bottom">
                <div class="compose-actions">
                  <button
                    v-if="latestReply || retryableTurn"
                    class="quiet regenerate-toggle"
                    :aria-expanded="rewriteExpanded"
                    :aria-label="rewriteExpanded ? '收起重新生成选项' : undefined"
                    :disabled="busy || !!editingMessageId || activeTurn?.status === 'awaiting_review'"
                    @click="rewriteExpanded = !rewriteExpanded"
                  >
                    ↻ 重新生成</button
                  ><EffortControl :value="quickEffort" :options="effortOptions" @change="setEffort" /><button
                    class="quiet"
                    @click="inspectContext"
                  >
                    ◫ 上下文</button
                  ><button class="quiet" @click="storyView = 'work'">
                    ◇ 后台记录 <span v-if="workCount">{{ workCount }}</span>
                  </button>
                </div>
                <span class="turn-options"
                  ><label
                    ><input
                      v-model="updateVariables"
                      type="checkbox"
                      :disabled="busy || activeTurn?.status === 'awaiting_review'"
                    />
                    本轮更新变量</label
                  ><label><input v-model="allowEdit" type="checkbox" /> 本轮允许修改设定</label></span
                ><button v-if="busy" class="send" @click="stop">■ 停止</button
                ><button
                  v-else
                  class="send"
                  :disabled="!input.trim() || activeTurn?.status === 'awaiting_review'"
                  @click="send()"
                >
                  发送
                </button>
              </div>
            </div>
            <p class="composer-meta">
              <span>Enter 发送 · Shift + Enter 换行</span><span>{{ actualInputLabel }}</span
              ><span>{{ contextLengthLabel }}</span
              ><span>缓存命中率 {{ cacheRateLabel }}</span
              ><span>剧情与变量一同保存</span>
            </p>
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
      :class="{ collapsed: layout.rightCollapsed, 'scene-active': inspectorTab === 'scene' }"
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
        ref="rightSidebarToggle"
        class="sidebar-collapse"
        @click="toggleRightSidebar"
        :aria-label="layout.rightCollapsed ? '展开右侧栏' : '折叠右侧栏'"
        :aria-expanded="!layout.rightCollapsed"
      >
        {{ layout.rightCollapsed ? '‹' : rightOverlay ? '×' : '›' }}
      </button>
      <div v-if="!layout.rightCollapsed" class="inspector-content">
        <nav class="inspector-tabs" aria-label="右侧栏页面">
          <button
            :class="{ active: inspectorTab === 'mvu' }"
            :aria-current="inspectorTab === 'mvu' ? 'page' : undefined"
            @click="inspectorTab = 'mvu'"
          >
            MVU 变量
          </button>
          <button
            :class="{ active: inspectorTab === 'scene' }"
            :aria-current="inspectorTab === 'scene' ? 'page' : undefined"
            @click="inspectorTab = 'scene'"
          >
            场景
          </button>
          <button
            :class="{ active: inspectorTab === 'options' }"
            :aria-current="inspectorTab === 'options' ? 'page' : undefined"
            @click="inspectorTab = 'options'"
          >
            会话设置
          </button>
        </nav>
        <section v-show="inspectorTab === 'mvu'" aria-label="MVU 变量">
          <h2>MVU 变量</h2>
          <StoryMessage
            v-if="mvuPanel"
            :message="mvuPanel"
            expand-first-level
            :session="session"
            :enabled="!!currentCard?.permission"
            @action="frameAction"
          />
          <p v-else class="state-empty">此角色卡暂无渲染面板，可展开查看变量。</p>
          <details :key="session.id">
            <summary>查看 MVU 变量</summary>
            <StateTree :value="session.snapshot.state.stat_data || session.snapshot.state" />
          </details>
        </section>
        <section v-show="inspectorTab === 'scene'" class="scene-memory" aria-label="场景">
          <div class="scene-heading">
            <h2>场景</h2>
            <button :disabled="busy || activeTurn?.status === 'awaiting_review'" @click="editSessionLore">
              编辑对话世界书
            </button>
          </div>
          <p class="inspector-note">
            这是当前对话的记忆上下文，由后台 Tracker 整理、Roleplay Runtime
            保存。记忆随剧情进度保存，继承进度时会带入新对话。
          </p>
          <p v-if="sceneUpdating" class="scene-update-status" role="status">
            {{ '正在根据当前对话重新生成记忆…' }}
          </p>
          <div class="scene-section-heading">
            <h3>记忆</h3>
            <button
              :disabled="
                busy || !!sceneSubmitting || !!editingMessageId || activeTurn?.status === 'awaiting_review'
              "
              :aria-busy="sceneUpdating === 'memory'"
              @click="refreshScene('memory')"
              aria-label="重新生成记忆"
            >
              <span v-if="sceneUpdating === 'memory'" class="scene-update-spinner" aria-hidden="true"></span
              >{{ sceneUpdating === 'memory' ? '更新中…' : '↻ 重新生成' }}
            </button>
          </div>
          <SceneNotes
            :key="`${session.id}:memory`"
            kind="memory"
            :snapshot="session.snapshot"
            :memory-view="session.memory_view"
            :progress="memoryProgress"
            @change="saveMemoryRecord"
            @cancel="stop"
            :messages="session.messages"
            :persona="session.persona"
            :character="archiveCharacter"
            :disabled="
              busy || !!sceneSubmitting || !!editingMessageId || activeTurn?.status === 'awaiting_review'
            "
            :saving="sceneSaving === 'memory'"
            @locate="locateSource"
          />
        </section>
        <section v-show="inspectorTab === 'options'" class="inspector-options" aria-label="会话设置">
          <div class="inspector-options-heading">
            <h2>会话设置</h2>
            <div class="inspector-save">
              <span v-if="optionsNotice" role="status">{{ optionsNotice }}</span>
              <button class="primary" @click="saveOptions">保存</button>
            </div>
          </div>
          <label>你的身份<input v-model="session.persona" /></label
          ><label
            >玩家完整描述<ExpandableTextarea
              v-model="session.persona_data.description"
              label="玩家完整描述"
              placeholder="外貌、背景、性格、能力与边界" /></label
          ><label
            >本轮偏好（可选）<ExpandableTextarea
              v-model="session.turn_preference"
              label="本轮偏好"
              placeholder="例如：多用对话推进，节奏放缓，给我留下行动空间。"
            />
          </label>
          <p class="inspector-note">仅补充当前对话的系统提示，保存后持续生效；清空可取消。</p>
          <PolicySelect v-model="session.policy" :inherited-policy="settings.policy" compact />
          <p class="inspector-note">
            世界书注入：{{
              session.lore_injection_mode === 'conversation' ? '随对话滚动' : 'ST 式顶部可变'
            }}（随剧情进度继承）。
          </p>
          <h3>上下文结构编排</h3>
          <p class="inspector-note">仅影响当前对话，随剧情进度继承。</p>
          <button
            :disabled="busy || savingRecipe || activeTurn?.status === 'awaiting_review'"
            @click="panel = 'recipe'"
          >
            编辑上下文配方</button
          ><button
            :disabled="busy || savingRecipe || activeTurn?.status === 'awaiting_review'"
            @click="resetRecipe"
          >
            恢复当前全局编排
          </button>
        </section>
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
        <h3 id="delete-confirm-title">删除“{{ deleteBubble.item.name }}”？</h3>
        <p>剧情分支、会话记忆、检索线索和运行记录都会彻底清除，且无法恢复。</p>
        <div>
          <button @click="closeDeleteBubble()">取消</button
          ><button class="danger" @click="deleteSession(deleteBubble.item)">彻底删除</button>
        </div>
      </section></Teleport
    >
    <Teleport to="body">
      <div v-if="panel" class="modal-backdrop">
        <AppModal
          :label="panel === 'settings' ? '设置' : '酒馆面板'"
          :size="
            ['settings', 'context', 'card', 'trace', 'recipe'].includes(panel)
              ? 'large'
              : ['delete-card', 'fork'].includes(panel)
                ? 'small'
                : 'medium'
          "
          @close="panel = ''"
          ><AppNotification :message="error" />
          <StartupWarmup
            v-if="panel === 'warmup'"
            :status="startupModels"
            @close="panel = ''"
            @settings="panel = 'settings'"
          />
          <SettingsPanel
            v-if="panel === 'settings'"
            :settings="settings"
            :saving="savingSettings"
            :effort-options="effortOptions"
            @save="saveSettings"
            @probe="probe"
          />
          <template v-else-if="panel === 'delete-card'"
            ><h2>删除角色卡</h2>
            <p>
              彻底删除“{{
                deletingCard?.name
              }}”？角色卡及未被使用的资源会删除，已有对话保留继续游玩所需的设定和资源。
            </p>
            <div class="modal-footer">
              <button :disabled="deletingCardBusy" @click="panel = ''">取消</button
              ><button class="danger" :disabled="deletingCardBusy" @click="deleteCard">
                {{ deletingCardBusy ? '正在删除…' : '确认删除角色卡' }}
              </button>
            </div></template
          >
          <RecipeEditor
            v-else-if="panel === 'recipe'"
            :recipe="session.recipe"
            :saving="savingRecipe"
            @save="saveRecipe"
          />
          <template v-else-if="panel === 'layout'"
            ><h2>阅读排版</h2>
            <label
              >正文字号<input
                aria-label="正文字号"
                type="number"
                min="14"
                max="28"
                v-model.number="layout.fontSize"
                @change="normalizeLayoutNumber('fontSize', 14, 28)" /></label
            ><label
              >行距<input
                aria-label="行距"
                type="number"
                min="1.4"
                max="2.6"
                step="0.1"
                v-model.number="layout.lineHeight"
                @change="normalizeLayoutNumber('lineHeight', 1.4, 2.6)" /></label
            ><label
              >阅读区宽度<input
                aria-label="阅读区宽度"
                type="number"
                min="700"
                max="1600"
                step="10"
                v-model.number="layout.readingWidth"
                @change="normalizeLayoutNumber('readingWidth', 700, 1600)" /></label
            ><label
              >右侧栏宽度<input
                aria-label="右侧栏宽度"
                type="number"
                min="240"
                max="640"
                step="10"
                v-model.number="layout.rightWidth"
                @change="normalizeLayoutNumber('rightWidth', 240, 640)" /></label
            ><label
              >字体<select v-model="layout.fontFamily">
                <option value="sans">清晰无衬线</option>
                <option value="serif">书籍衬线</option>
              </select></label
            ><label class="check"><input type="checkbox" v-model="layout.leftCollapsed" />折叠左侧栏</label
            ><label class="check"><input type="checkbox" v-model="layout.rightCollapsed" />折叠右侧栏</label>
            <p class="muted">设置即时生效，保存在当前浏览器。</p></template
          ><template v-else-if="panel === 'memory-rebuild'">
            <h2>重新生成记忆</h2>
            <p>
              预计至少 {{ memoryRebuildEstimate?.estimated_batches }} 次分批整理，最多
              {{ memoryRebuildEstimate?.review_max_calls }} 次疑难复核。
            </p>
            <p class="muted">
              {{ memoryRebuildEstimate?.message }}
              重建会重新核对当前分支全部原文，保留手工修订和删除标记。实际 API 用量在后台工作记录中查看。
            </p>
            <div class="actions">
              <button :disabled="busy || !!sceneSubmitting" @click="startSceneRefresh('memory')">
                开始重建
              </button>
              <button @click="panel = ''">取消</button>
            </div></template
          ><template v-else-if="panel === 'state'"
            ><h2>状态与记忆</h2>
            <p class="muted">仅显示当前剧情分支。切换分支会一同恢复状态。</p>
            <h3>当前状态</h3>
            <StateTree :value="session.snapshot.state.stat_data || session.snapshot.state" />
            <h3>记忆</h3>
            <SceneNotes
              kind="memory"
              :snapshot="session.snapshot"
              :memory-view="session.memory_view"
              :progress="memoryProgress"
              @change="saveMemoryRecord"
              @cancel="stop"
              :messages="session.messages"
              :persona="session.persona"
              :character="archiveCharacter"
              :disabled="busy || activeTurn?.status === 'awaiting_review'"
              :saving="sceneSaving === 'memory'"
              @locate="locateSource" />
            <h3>设定演化</h3>
            <StateTree :value="session.snapshot.content"
          /></template>
          <template v-else-if="panel === 'fork-select'"
            ><h2>新建对话</h2>
            <p class="muted">
              选择要继承的对话进度。新对话将从所选回复结束的位置继续，并保留该位置的记忆与状态快照。
            </p>
            <div class="branch-list">
              <button
                v-for="(point, index) in forkPoints"
                :key="point.id"
                class="branch"
                :class="{ current: point.id === session.head }"
                :title="point.raw"
                @click="askFork(point, true)"
              >
                <small class="branch-index">{{ index + 1 }}</small
                ><span class="branch-role">{{ name }}</span
                ><span class="branch-excerpt">{{
                  point.raw.replace(/\s+/g, ' ').trim() || '（空回复）'
                }}</span
                ><small v-if="point.id === session.head" class="branch-current">当前</small>
              </button>
            </div>
            <p v-if="!forkPoints.length" class="muted">暂无可继承的 AI 回复。</p>
            <div class="modal-footer"><button @click="panel = ''">取消</button></div></template
          >
          <template v-else-if="panel === 'fork'"
            ><h2>继承进度，新建对话</h2>
            <p class="muted">
              继承这条回复及此前的完整对话，同时延续当时的场景变量、记忆、检索线索、世界书和脚本变量，以及上下文配方。新对话从这里独立继续，原对话保留。
            </p>
            <div class="fork-preview">
              <strong>{{ name }}</strong>
              <p>{{ forkMessage?.raw }}</p>
            </div>
            <div class="modal-footer">
              <button v-if="forkFromPicker" :disabled="creatingFork" @click="panel = 'fork-select'">
                返回选择</button
              ><button :disabled="creatingFork" @click="panel = ''">取消</button
              ><button class="primary" :disabled="creatingFork" @click="createFork">
                {{ creatingFork ? '正在创建…' : '确认继承并新建对话' }}
              </button>
            </div></template
          >
          <template v-else-if="panel === 'context'"
            ><h2>上下文预览</h2>
            <p>{{ actualInputLabel }} · {{ contextLengthLabel }}</p>
            <p class="muted">{{ tokenizerLabel }}</p>
            <p class="muted">
              日常输入预算（tokens）
              {{ preview?.daily_context_budget?.toLocaleString() }}
              tokens；预览不会加载神经模型，下方诊断会注明不完整的召回。
            </p>
            <div class="budget-sources">
              <span v-for="(tokens, source) in preview?.budget_sources" :key="source"
                >{{ contextLabel(String(source)) }}
                {{ Math.round((Number(tokens) / Math.max(1, preview.estimated_tokens)) * 100) }}%</span
              >
            </div>
            <div class="context-overview">
              <div>
                <small>预计输入（tokens）</small
                ><strong>{{ Number(preview?.estimated_tokens || 0).toLocaleString() }} tokens</strong>
              </div>
              <div>
                <small>模型上下文窗口（tokens）</small
                ><strong>{{ Number(settings.context_tokens || 0).toLocaleString() }} tokens</strong>
              </div>
              <div>
                <small>内容块</small><strong>{{ preview?.trace?.length || 0 }}</strong>
              </div>
              <div>
                <small>缓存命中率</small><strong>{{ cacheRateLabel }}</strong>
              </div>
            </div>
            <div class="context-sequence" aria-label="上下文结构">
              <span
                v-for="group in contextGroups"
                :key="group.id"
                class="context-chip"
                :class="`context-${group.kind}`"
                >{{ group.label }} · {{ group.count }}</span
              >
            </div>
            <div class="context-groups">
              <details
                v-for="group in contextGroups"
                :key="group.id"
                class="context-group"
                open
                :class="`context-${group.kind}`"
              >
                <summary>
                  <span class="context-dot"></span><strong>{{ group.label }}</strong
                  ><small
                    >{{ group.source }} · {{ group.count }} 条 · {{ group.items.length }} 块 · 预计
                    {{ group.tokens.toLocaleString() }} tokens</small
                  >
                </summary>
                <div class="context-items">
                  <details v-for="item in group.items" :key="item.position" class="context-entry">
                    <summary>
                      <strong class="context-entry-title" :title="contextItemTitle(item)">{{
                        contextItemTitle(item)
                      }}</strong
                      ><span class="badge">{{ item.role }}</span
                      ><span class="context-entry-position">位置 {{ item.position }}</span
                      ><small>预计 {{ Number(item.estimated_tokens || 0).toLocaleString() }} tokens</small>
                    </summary>
                    <pre>{{ item.content }}</pre>
                  </details>
                </div>
              </details>
            </div>
            <details class="context-diagnostics">
              <summary>
                触发与裁剪
                <small
                  >{{ preview?.diagnostics?.length || 0 }} 条诊断 ·
                  {{ preview?.lore?.length || 0 }} 条世界书命中</small
                >
              </summary>
              <pre>{{
                JSON.stringify(
                  { lore: preview?.lore, diagnostics: preview?.diagnostics, retrieval: preview?.retrieval },
                  null,
                  2,
                )
              }}</pre>
            </details></template
          >
          <template v-else-if="panel === 'trace'"
            ><h2>幕后活动</h2>
            <p class="muted">模型请求、工具活动、变量修改与脚本日志。</p>
            <details v-for="(event, i) in trace" :key="i">
              <summary>{{ event.kind }}</summary>
              <pre>{{ JSON.stringify(event.payload, null, 2) }}</pre>
            </details>
            <p v-if="!trace.length" class="muted">下一轮运行时，活动将在这里出现。</p></template
          >
          <template v-else-if="panel === 'probe'"
            ><h2>模型能力检查</h2>
            <pre>{{ JSON.stringify(preview, null, 2) }}</pre>
          </template>
          <template v-else-if="panel === 'about'"
            ><div class="eyebrow">TERRARIUM TAVERN / 0.1</div>
            <h2>给故事一处生长的地方。</h2>
            <p>本地优先的 Roleplay Agent 应用。角色数据、剧情分支和记忆保存在你的电脑。</p>
            <p>
              Powered by
              <a href="https://github.com/Kohaku-Lab/KohakuTerrarium" target="_blank" rel="noreferrer"
                >KohakuTerrarium</a
              >
              · Shih-Ying Yeh 与贡献者。
            </p>
            <p>兼容接口参考 Tavern Helper 与 MagVarUpdate。支持范围以项目兼容矩阵和测试为准。</p></template
          >
        </AppModal>
      </div>
    </Teleport>
  </div>
</template>
