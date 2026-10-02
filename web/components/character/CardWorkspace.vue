<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { compare } from 'fast-json-patch';
import { api } from '../../lib/api';
import { cardName, mentionCard, type EditorCard, type EditorConversation } from '../../lib/cardEditor';
import {
  editorActions,
  actionStatus,
  mergeEditorEvents,
  type EditorEvent,
} from '../../lib/cardAgentActivity';
import { eventCursor } from '../../lib/turnStream';
import StoryProse from '../story/StoryProse.vue';
import JsonDataTree from '../common/JsonDataTree.vue';
import AppModal from '../common/AppModal.vue';
import CharacterEditor from './CharacterEditor.vue';

const props = defineProps<{ conversationId: string; library: any[] }>();
const emit = defineEmits<{ changed: []; saved: []; deleted: [id: string] }>();
const workspace = ref<EditorConversation | null>(null);
const input = ref(sessionStorage.getItem(`tavern-editor-input-${props.conversationId}`) || '');
const error = ref(''),
  notice = ref(''),
  busy = ref(false),
  partial = ref('');
const selected = ref(''),
  tab = ref('document'),
  inspectorOpen = ref(false),
  pendingView = ref(false);
const modal = ref(''),
  search = ref(''),
  title = ref(''),
  jsonPreview = ref<any>(null),
  probe = ref<any>(null);
const composer = ref<HTMLTextAreaElement>(),
  searchInput = ref<HTMLInputElement>(),
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
let stream: EventSource | undefined;
let modalTrigger: HTMLElement | null = null;
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
const filtered = computed(() =>
  props.library.filter((card) => card.name.toLocaleLowerCase().includes(search.value.toLocaleLowerCase())),
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
    if (/(?:^|\s)@$/.test(value) && !locked.value && !modal.value) void openModal('references');
  },
  { flush: 'sync' },
);
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
  const value = (await api(base)) as EditorConversation;
  if (!alive) return;
  workspace.value = value;
  if (!value.pending) pendingView.value = false;
  emit('changed');
}
async function loadActivity(id: string) {
  let cursor = 0;
  do {
    const page = await api(`/card-edit-runs/${id}/activity?after=${cursor}`);
    if (!alive) return;
    activities.value[id] = mergeEditorEvents(activities.value[id] || [], page.events);
    if (!page.has_more) break;
    cursor = page.next_cursor;
  } while (alive);
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
function showRun(id: string, event: Event) {
  if ((event.target as HTMLDetailsElement).open && !activities.value[id])
    void loadActivity(id).catch((e: Error) => {
      if (alive) error.value = e.message;
    });
}
function observe(id: string) {
  stream?.close();
  const accept = eventCursor();
  partial.value = '';
  const connection = new EventSource(`/api/card-edit-runs/${id}/events`);
  stream = connection;
  connection.onmessage = (message) => {
    if (!alive || connection !== stream) return;
    const event = JSON.parse(message.data) as EditorEvent;
    if (!accept(event)) return;
    if (event.kind === 'text') partial.value += event.payload.text;
    if (event.kind === 'model_call') partial.value = '';
    if (event.kind === 'context_warning') notice.value = event.payload.message;
    if (['model_call', 'tool_started', 'tool_finished'].includes(event.kind))
      activities.value[id] = mergeEditorEvents(activities.value[id] || [], [event]);
    scroll();
  };
  connection.addEventListener('end', () => {
    connection.close();
    if (!alive || connection !== stream) return;
    void guard(async () => {
      await refresh();
      partial.value = '';
      await loadActivity(id);
      scroll();
    });
  });
}
async function send(resume = false) {
  if (!workspace.value || busy.value || active.value || (!resume && !input.value.trim())) return;
  await guard(async () => {
    const run = await api(base + '/runs', {
      text: input.value,
      expected_revision: workspace.value!.revision,
      operation_id: crypto.randomUUID(),
      resume_run_id: resume ? workspace.value!.pending!.run : null,
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
  if (!modal.value) modalTrigger = document.activeElement as HTMLElement | null;
  modal.value = name;
  if (name === 'rename') title.value = workspace.value?.title || '';
  await nextTick();
  const target =
    (name === 'references' ? searchInput.value : dialogInput.value) ||
    modalOverlay.value?.querySelector<HTMLElement>('button:not([disabled])');
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
async function attach(card: any) {
  await guard(async () => {
    const value = await api(base + '/references', {
      card_id: card.id,
      expected_revision: workspace.value!.revision,
      expected_card_revision: card.revision,
    });
    if (!alive) return;
    workspace.value = value;
    const item = value.cards.find((c: EditorCard) => c.card === card.id);
    selected.value = item.ref;
    closeModal();
    input.value = mentionCard(input.value, item.ref);
    emit('changed');
  });
}
function mention(item: EditorCard) {
  closeModal();
  input.value = mentionCard(input.value, item.ref);
  selected.value = item.ref;
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
  await guard(async () => {
    await api(base + `/drafts/${ref}`, { document, expected_revision: workspace.value!.revision }, 'PUT');
    if (!alive) return;
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
async function showPreview() {
  await guard(async () => {
    const value = await api(base + '/preview', { text: input.value });
    if (alive) {
      jsonPreview.value = value;
      void openModal('preview');
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
  });
});
onBeforeUnmount(() => {
  alive = false;
  stream?.close();
  window.removeEventListener('resize', onResize);
  const background = shell.value?.closest<HTMLElement>('.shell');
  if (background) background.inert = false;
});
</script>

<template>
  <section ref="shell" class="card-workspace" aria-label="角色卡 Agent" :inert="!!modal">
    <header class="cw-header">
      <div>
        <small>CARD EDITOR</small>
        <h1>{{ workspace?.title || '角色卡 Agent' }}</h1>
      </div>
      <div class="cw-toolbar">
        <button :disabled="locked" @click="openModal('rename')">改名</button>
        <button :disabled="locked" @click="openModal('delete')">删除对话</button>
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
    <div class="cw-body">
      <div class="cw-dialogue" :inert="inspectorOpen && narrow">
        <div class="cw-chat-wrap">
          <div ref="chat" class="cw-chat" aria-label="制卡对话" @scroll="trackScroll">
            <div v-if="workspace && !workspace.messages.length" class="cw-empty">
              <h2>从一个想法开始</h2>
              <p>讨论人物、世界或互动情境，也可以引用书架中的角色卡来分析和修改。</p>
              <p>制作新卡时，先讨论方案，确认后生成可审阅的草稿。</p>
            </div>
            <article
              v-for="message in workspace?.messages"
              :key="message.ref"
              class="cw-message"
              :class="message.role"
            >
              <small>{{ message.role === 'user' ? '你' : 'Card Editor' }} · {{ message.ref }}</small>
              <StoryProse :text="message.content" />
            </article>
            <article v-if="active" class="cw-message" aria-live="polite">
              <small>Card Editor · 处理中</small><StoryProse v-if="partial" :text="partial" />
              <p v-else class="muted">正在读取资料或处理工具…</p>
            </article>
            <details
              v-for="run in workspace?.runs"
              :key="run.id"
              class="cw-run"
              :open="run.id === active?.id"
              @toggle="showRun(run.id, $event)"
            >
              <summary>{{ run.request.text || '继续本轮' }} · {{ actionStatus(run.status) }}</summary>
              <p v-if="run.error" class="danger">{{ run.error }}</p>
              <details
                v-for="step in editorActions(activities[run.id] || [], run.status)"
                :key="step.id"
                class="cw-step"
              >
                <summary>{{ step.title }} · {{ step.target }} · {{ actionStatus(step.status) }}</summary>
                <pre v-if="step.arguments">{{ step.arguments }}</pre>
                <pre v-if="step.result">{{ step.result }}</pre>
                <small v-if="step.truncated">这里只展示日志摘要；模型读取的原文保持完整。</small>
              </details>
            </details>
          </div>
          <button v-if="!followingLatest" class="cw-latest" aria-label="跳到最新" @click="scroll(true)">
            ↓
          </button>
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
          <div class="cw-references">
            <button type="button" :disabled="locked" @click="openModal('references')">＋ 引用角色卡</button>
            <button v-for="item in workspace?.cards" :key="item.ref" type="button" @click="mention(item)">
              @{{ item.ref }} {{ cardName(item) }}
            </button>
          </div>
          <textarea
            ref="composer"
            v-model="input"
            aria-label="制卡要求"
            :disabled="busy || !!active"
            :placeholder="
              workspace?.pending
                ? '可补充新要求后继续，例如：保留 A 的改动，恢复 B…'
                : '描述想法，或输入 @ 引用角色卡。Enter 发送，Shift+Enter 换行。'
            "
            @keydown.enter.exact.prevent="!$event.isComposing && send(!!workspace?.pending)"
          />
          <div class="cw-composer-actions">
            <button type="button" :disabled="busy" @click="showPreview">工具与上下文</button>
            <button v-if="active" type="button" @click="stop">停止</button>
            <button v-else class="primary" :disabled="busy || (!input.trim() && !workspace?.pending)">
              {{ workspace?.pending ? (input.trim() ? '补充要求并继续' : '继续本轮') : '发送' }}
            </button>
          </div>
        </form>
      </div>
      <aside class="cw-inspector" :class="{ open: inspectorOpen }" aria-label="卡片与修改">
        <button class="cw-close-inspector" @click="inspectorOpen = false">返回对话</button>
        <div class="cw-inspector-top">
          <label
            >当前卡片<select v-model="selected" aria-label="选择卡片">
              <option v-for="item in items" :key="item.ref" :value="item.ref">
                {{ item.ref }} · {{ cardName(item) }}
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
                { id: 'document', name: '资料' },
                { id: 'changes', name: '修改' },
                { id: 'validation', name: '诊断' },
              ]"
              :key="t.id"
              :aria-pressed="tab === t.id"
              @click="tab = t.id"
            >
              {{ t.name }}{{ t.id === 'changes' ? ` ${changes.length}` : '' }}
            </button>
          </nav>
          <div class="cw-inspector-scroll">
            <template v-if="tab === 'document'">
              <p class="muted">
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
              <button :disabled="locked || pendingView" @click="openModal('form')">打开表单编辑</button>
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
                <button
                  v-if="!current.card"
                  class="primary"
                  :disabled="locked || pendingView"
                  @click="action('create')"
                >
                  保存新卡
                </button>
                <button
                  v-else
                  class="primary"
                  :disabled="locked || pendingView || !current.source_exists"
                  @click="action('overwrite')"
                >
                  覆盖原卡
                </button>
                <button :disabled="locked || pendingView" @click="action('copy')">另存副本</button>
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
        </template>
        <p v-else class="cw-empty">还没有引用或草稿。可以直接开始讨论。</p>
      </aside>
    </div>
  </section>
  <Teleport to="body"
    ><div v-if="modal" ref="modalOverlay" class="modal-backdrop cw-overlay" @keydown.esc="closeModal">
      <AppModal
        :label="modal === 'form' ? '编辑角色卡草稿' : modal === 'references' ? '引用角色卡' : '制卡对话操作'"
        :size="['form', 'preview'].includes(modal) ? 'large' : modal === 'references' ? 'medium' : 'small'"
        @close="closeModal"
      >
        <template v-if="modal === 'references'"
          ><h2>引用角色卡</h2>
          <label>搜索书架<input ref="searchInput" v-model="search" /></label>
          <div class="cw-picker">
            <button v-for="card in filtered" :key="card.id" :disabled="busy" @click="attach(card)">
              {{ card.name }}
            </button>
          </div>
          <p v-if="!filtered.length" class="muted">没有匹配的书架角色卡。也可以直接描述想制作的新卡。</p>
        </template>
        <CharacterEditor
          v-else-if="modal === 'form' && formCard"
          :key="formCard.id"
          :card="formCard"
          draft-mode
          @draft-saved="saveForm"
          @back="closeModal"
        />
        <template v-else-if="modal === 'preview'"
          ><h2>工具与上下文</h2>
          <p>{{ jsonPreview?.note }}</p>
          <p>估算 {{ jsonPreview?.estimated_tokens }} / {{ jsonPreview?.context_limit }} tokens</p>
          <JsonDataTree :value="jsonPreview"
        /></template>
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

<style scoped>
.card-workspace {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.cw-header {
  height: auto;
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 16px 24px 16px 56px;
  border-bottom: 1px solid var(--border-subtle);
}
.cw-header small {
  color: var(--text-faint);
  letter-spacing: 0.12em;
  font-size: 10px;
}
.cw-header h1 {
  font-size: 19px;
  margin: 4px 0 0;
  overflow-wrap: anywhere;
}
.cw-toolbar,
.cw-composer-actions,
.cw-references {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.cw-body {
  min-height: 0;
  flex: 1;
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(300px, 36%);
}
.cw-dialogue {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.cw-chat-wrap {
  position: relative;
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}
.cw-latest {
  position: absolute;
  right: 20px;
  bottom: 16px;
  border-radius: 50%;
  width: 38px;
  height: 38px;
  background: var(--surface-active);
}
.cw-chat {
  overflow: auto;
  flex: 1;
  min-height: 0;
  padding: 24px clamp(16px, 4vw, 64px);
}
.cw-empty {
  color: var(--text-secondary);
  line-height: 1.8;
  padding: 30px 18px;
}
.cw-empty h2 {
  color: var(--text-primary);
  font-size: 24px;
}
.cw-message {
  padding: 12px 0 24px;
  overflow-wrap: anywhere;
}
.cw-message > small {
  color: var(--text-muted);
  display: block;
  margin-bottom: 10px;
}
.cw-message.user {
  padding: 16px;
  margin-bottom: 14px;
  background: var(--surface-panel);
  border-radius: 10px;
}
.cw-composer {
  padding: 12px 20px 18px;
  border-top: 1px solid var(--border-subtle);
}
.cw-composer textarea {
  width: 100%;
  min-height: 86px;
  max-height: 180px;
  resize: vertical;
  margin: 10px 0;
}
.cw-composer-actions {
  justify-content: space-between;
}
.cw-references {
  max-height: 80px;
  overflow: auto;
}
.cw-references button {
  font-size: 12px;
}
.cw-notice {
  padding: 8px 16px;
  margin: 0;
  color: var(--warning);
  background: var(--surface-panel);
  font-size: 13px;
}
.cw-pending {
  padding: 12px 20px;
  border-top: 1px solid var(--border-control);
  font-size: 13px;
}
.cw-pending p {
  margin: 6px 0;
}
.cw-inspector {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  border-left: 1px solid var(--border-subtle);
  background: var(--surface-panel);
}
.cw-inspector-top {
  padding: 16px;
}
.cw-inspector select {
  width: 100%;
}
.cw-tabs {
  display: flex;
  gap: 6px;
  padding: 0 16px 12px;
  border-bottom: 1px solid var(--border-subtle);
}
.cw-tabs button[aria-pressed='true'] {
  background: var(--accent-soft);
}
.cw-inspector-scroll {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 16px;
}
.cw-inspector-scroll details {
  margin: 12px 0;
}
.cw-save-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 16px;
}
.cw-run {
  border-top: 1px solid var(--border-subtle);
  padding: 10px 0;
  font-size: 12px;
}
.cw-run > summary {
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.cw-step {
  padding: 8px;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 12px;
}
.cw-inspector-toggle,
.cw-close-inspector {
  display: none;
}
.cw-picker {
  display: grid;
  gap: 8px;
  padding-top: 16px;
}
.cw-overlay :deep(.editor-page) {
  height: min(70vh, 800px);
}
@media (max-width: 1100px) {
  .cw-body {
    grid-template-columns: minmax(0, 1fr);
    position: relative;
  }
  .cw-inspector {
    display: none;
  }
  .cw-inspector.open {
    display: flex;
    position: absolute;
    inset: 0;
    z-index: 5;
  }
  .cw-inspector-toggle,
  .cw-close-inspector {
    display: inline-block;
  }
  .cw-close-inspector {
    margin: 12px 16px 0;
    align-self: flex-start;
  }
  .cw-header {
    padding: 14px 16px 14px 56px;
  }
  .cw-header h1 {
    font-size: 16px;
  }
  .cw-toolbar {
    gap: 4px;
  }
  .cw-header button {
    font-size: 12px;
    padding: 6px 8px;
  }
}
@media (max-width: 600px) {
  .cw-header {
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
  }
}
</style>
