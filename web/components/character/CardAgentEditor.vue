<script setup lang="ts">
import { computed, nextTick, onMounted, onBeforeUnmount, ref, watch } from 'vue';
import { compare } from 'fast-json-patch';
import { api } from '../../lib/api';
import { eventCursor } from '../../lib/turnStream';
import JsonDataTree from '../common/JsonDataTree.vue';
import StoryProse from '../story/StoryProse.vue';
import DeleteEditorDialog from './DeleteEditorDialog.vue';
import {
  actionStatus,
  editorActions,
  mergeEditorEvents,
  type EditorEvent,
} from '../../lib/cardAgentActivity';
const props = defineProps<{ workspace: any }>();
const emit = defineEmits<{
  changed: [workspace: any];
  saved: [card: any];
  discarded: [];
  running: [value: boolean, workspaceId: string];
  back: [];
  selectWorkspace: [workspace: any];
}>();
const input = ref(''),
  error = ref(''),
  partial = ref(''),
  active = ref<any>(null),
  busy = ref(false);
const contexts = ref<any[]>([]),
  changingContext = ref(false);
const inputKey = `tavern-editor-input-${props.workspace.id}`;
try {
  input.value = sessionStorage.getItem(inputKey) || '';
} catch {
  /* Storage may be unavailable. */
}
watch(
  input,
  (value) => {
    try {
      if (value) sessionStorage.setItem(inputKey, value);
      else sessionStorage.removeItem(inputKey);
    } catch {
      /* Keep the in-memory input. */
    }
  },
  { flush: 'sync' },
);
const contextControlsDisabled = computed(() => changingContext.value || (busy.value && !active.value));
function contextLabel(context: any) {
  const title = context.title || '新对话';
  const status = (
    {
      queued: '等待中',
      running: '运行中',
      error: '失败',
      interrupted: '已中断',
      cancelled: '已取消',
    } as Record<string, string>
  )[context.status];
  return `${title.slice(0, 24)}${title.length > 24 ? '…' : ''} · ${context.id.slice(-4)}${status ? ' · ' + status : ''}`;
}
async function refreshContexts() {
  try {
    const values = await api(`/cards/${props.workspace.card}/edits`);
    if (alive) contexts.value = values;
  } catch (e: any) {
    if (alive) error.value = e.message;
  }
}
async function chooseContext(id: string) {
  if (id === props.workspace.id || contextControlsDisabled.value) return;
  changingContext.value = true;
  await guarded(async () => {
    const selected = await api(`/card-edits/${id}`);
    if (alive) emit('selectWorkspace', selected);
  });
  if (alive) changingContext.value = false;
}
async function newContext(carryDraft: boolean) {
  if (contextControlsDisabled.value) return;
  changingContext.value = true;
  await guarded(async () => {
    let selected;
    if (carryDraft)
      selected = await api(`/card-edits/${props.workspace.id}/new-context`, {
        expected_revision: props.workspace.revision,
      });
    else {
      const saved = await api(`/cards/${props.workspace.card}`);
      selected = await api(`/cards/${saved.id}/edits`, { expected_revision: saved.revision });
    }
    if (alive) emit('selectWorkspace', selected);
  });
  if (alive) changingContext.value = false;
}
watch(
  () => props.workspace.runs.map((run: any) => `${run.id}:${run.status}`).join('|'),
  () => void refreshContexts(),
);
let stream: EventSource | undefined;
let alive = true;
const changes = computed(() => compare(props.workspace.base_document, props.workspace.document));
const deleteDialog = ref(false),
  deleteBusy = ref(false),
  deleteError = ref('');
const dialogBackground = computed(() => root.value?.closest<HTMLElement>('.shell'));
function requestDelete() {
  if (busy.value || changingContext.value) return;
  deleteError.value = '';
  deleteDialog.value = true;
}
async function deleteContext(saveMode?: 'overwrite' | 'copy') {
  if (deleteBusy.value) return;
  deleteBusy.value = true;
  deleteError.value = '';
  busy.value = true;
  emit('running', true, props.workspace.id);
  try {
    const base = `/card-edits/${props.workspace.id}`;
    let revision = props.workspace.revision;
    if (saveMode) {
      const card = await api(base + '/save', {
        expected_revision: revision,
        mode: saveMode,
        operation_id: crypto.randomUUID(),
      });
      const workspace = await api(base);
      const savedRevision = revision + 1;
      revision = workspace.revision;
      if (alive) {
        emit('changed', workspace);
        emit('saved', card);
      }
      if (revision !== savedRevision) throw Error('保存后草稿又有新的修改，对话未删除。请重新检查。');
    }
    await api(base, { expected_revision: revision }, 'DELETE');
    sessionStorage.removeItem(inputKey);
    sessionStorage.removeItem(`tavern-card-draft-${props.workspace.id}`);
    if (alive) {
      deleteDialog.value = false;
      emit('discarded');
    }
  } catch (e: any) {
    if (alive) deleteError.value = e.message;
  } finally {
    if (alive) {
      deleteBusy.value = false;
      busy.value = false;
      emit('running', false, props.workspace.id);
    }
  }
}
function pointerValue(document: any, path: string) {
  return path
    .split('/')
    .slice(1)
    .reduce((value, key) => value?.[key.replace(/~1/g, '/').replace(/~0/g, '~')], document);
}
function changeTitle(path: string) {
  const parts = path
    .split('/')
    .filter(Boolean)
    .filter((part) => part !== 'data');
  const labels: Record<string, string> = {
    name: '角色名称',
    description: '角色描述',
    personality: '性格',
    scenario: '场景',
    first_mes: '默认开场',
    alternate_greetings: '候选开场',
    extensions: '扩展与工作流',
    character_book: '世界书',
  };
  const prefix = labels[parts[0]] || parts[0];
  if (parts[0] === 'character_book' && parts[1] === 'entries') {
    const root = props.workspace.document.data || props.workspace.document;
    const entry = root.character_book?.entries?.[parts[2]];
    return `${prefix} · ${entry?.comment || entry?.name || parts[2]}`;
  }
  return prefix;
}
const validation = computed(() => props.workspace.validation);
const root = ref<HTMLElement>(),
  conversation = ref<HTMLElement>();
const inspectorTab = ref('data'),
  inspectorOpen = ref(false),
  narrow = ref(false),
  treeVersion = ref(0);
const following = ref(true),
  reconnecting = ref(false),
  needsReload = ref(false);
const activity = ref<Record<string, EditorEvent[]>>({}),
  activityErrors = ref<Record<string, string>>({});
const inspectorToggle = ref<HTMLButtonElement>();
let resize: ResizeObserver | undefined;
const cardName = computed(() => (props.workspace.document.data || props.workspace.document).name);
const runs = computed(() => {
  const values = [...props.workspace.runs];
  if (active.value) {
    const index = values.findIndex((run: any) => run.id === active.value.id);
    if (index < 0) values.push(active.value);
    else values[index] = active.value;
  }
  return values;
});
const toolFailures = computed(() =>
  runs.value.flatMap((run: any) =>
    editorActions(activity.value[run.id] || [], run.status).filter((step) => step.status === 'error'),
  ),
);
const tabs = computed(() => [
  { id: 'data', label: '角色数据' },
  {
    id: 'diagnostics',
    label: '诊断',
    count: validation.value.errors.length + validation.value.warnings.length + toolFailures.value.length,
  },
  { id: 'changes', label: '修改', count: changes.value.length },
]);
const actionsFor = (run: any) => editorActions(activity.value[run.id] || [], run.status);
function appendEvents(id: string, events: EditorEvent[]) {
  activity.value[id] = mergeEditorEvents(activity.value[id] || [], events);
  void scrollToLatest();
}
async function hydrate(run: any) {
  delete activityErrors.value[run.id];
  let cursor = 0;
  try {
    while (alive) {
      const page = await api(`/card-edit-runs/${run.id}/activity?after=${cursor}`);
      if (!alive) return;
      appendEvents(run.id, page.events);
      if (!page.has_more) return;
      cursor = page.next_cursor;
    }
  } catch (e: any) {
    if (alive) activityErrors.value[run.id] = e.message;
  }
}
async function scrollToLatest(force = false) {
  await nextTick();
  if (!alive || (!following.value && !force)) return;
  if (conversation.value) conversation.value.scrollTop = conversation.value.scrollHeight;
  if (force) following.value = true;
}
function trackScroll() {
  const el = conversation.value;
  if (el) following.value = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
}
async function closeInspector() {
  inspectorOpen.value = false;
  await nextTick();
  inspectorToggle.value?.focus();
}
async function openInspector() {
  inspectorOpen.value = true;
  await nextTick();
  root.value?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')?.focus();
}
function moveTab(event: KeyboardEvent, direction: number) {
  const parent = (event.currentTarget as HTMLElement).parentElement;
  const index = tabs.value.findIndex((tab) => tab.id === inspectorTab.value);
  inspectorTab.value = tabs.value[(index + direction + tabs.value.length) % tabs.value.length].id;
  nextTick(() => parent?.querySelector<HTMLElement>('[aria-selected="true"]')?.focus());
}
async function guarded(fn: () => Promise<void>) {
  error.value = '';
  try {
    await fn();
  } catch (e: any) {
    if (alive) error.value = e.message;
  }
}
async function refreshAfterRun() {
  await guarded(async () => {
    try {
      const workspace = await api(`/card-edits/${props.workspace.id}`);
      if (!alive) return;
      emit('changed', workspace);
      busy.value = false;
      emit('running', false, props.workspace.id);
      active.value = null;
      partial.value = '';
      needsReload.value = false;
      void scrollToLatest();
    } catch (e) {
      needsReload.value = true;
      throw e;
    }
  });
}
function observe(run: any) {
  stream?.close();
  void refreshContexts();
  active.value = run;
  busy.value = true;
  emit('running', true, props.workspace.id);
  partial.value = '';
  needsReload.value = false;
  const accept = eventCursor();
  stream = new EventSource(`/api/card-edit-runs/${run.id}/events`);
  stream.onopen = () => {
    if (alive) reconnecting.value = false;
  };
  stream.onerror = () => {
    if (alive) reconnecting.value = true;
  };
  stream.onmessage = (message) => {
    if (!alive) return;
    const event = JSON.parse(message.data);
    if (!accept(event)) return;
    if (event.kind === 'text') partial.value += event.payload.text;
    else if (
      ['started', 'model_call', 'tool_started', 'tool_finished', 'completed', 'error', 'cancelled'].includes(
        event.kind,
      )
    )
      appendEvents(run.id, [event]);
    if (event.kind === 'error') error.value = event.payload.message;
    void scrollToLatest();
  };
  stream.addEventListener('end', () => {
    stream?.close();
    reconnecting.value = false;
    if (alive) void refreshAfterRun();
  });
}
async function send(adapt = false) {
  if (busy.value || changingContext.value || (!adapt && !input.value.trim())) return;
  await guarded(async () => {
    busy.value = true;
    emit('running', true, props.workspace.id);
    try {
      const run = await api(`/card-edits/${props.workspace.id}/runs`, {
        text: adapt
          ? '请适配本项目工作流，按内置 skill 检查全部开场、世界书规则与脚本，保留叙事和初始状态。'
          : input.value,
        adapt,
        expected_revision: props.workspace.revision,
        operation_id: crypto.randomUUID(),
      });
      if (!alive) return;
      input.value = '';
      observe(run);
      void scrollToLatest(true);
    } catch (e) {
      busy.value = false;
      emit('running', false, props.workspace.id);
      throw e;
    }
  });
}
async function action(kind: 'undo' | 'reload' | 'overwrite' | 'copy') {
  await guarded(async () => {
    busy.value = true;
    emit('running', true, props.workspace.id);
    try {
      const base = `/card-edits/${props.workspace.id}`;
      const revision = { expected_revision: props.workspace.revision };
      if (kind === 'undo' || kind === 'reload') {
        const value = await api(base + '/' + kind, revision);
        if (alive) emit('changed', value);
      } else {
        const card = await api(base + '/save', {
          ...revision,
          mode: kind,
          operation_id: crypto.randomUUID(),
        });
        const workspace = await api(base);
        if (alive) {
          emit('changed', workspace);
          emit('saved', card);
        }
      }
    } finally {
      if (alive) {
        busy.value = false;
        emit('running', false, props.workspace.id);
      }
    }
  });
}
onMounted(() => {
  void refreshContexts();
  resize = new ResizeObserver(([entry]) => {
    narrow.value = entry.contentRect.width < 850;
  });
  if (root.value) {
    narrow.value = root.value.clientWidth < 850;
    resize.observe(root.value);
  }
  for (const run of props.workspace.runs) void hydrate(run);
  const running = props.workspace.runs.find((run: any) => ['queued', 'running'].includes(run.status));
  if (running) observe(running);
  void scrollToLatest(true);
});
onBeforeUnmount(() => {
  alive = false;
  stream?.close();
  resize?.disconnect();
});
</script>
<template>
  <section
    ref="root"
    class="agent-editor"
    aria-label="角色卡 Agent 编辑"
    @keydown.esc="narrow && inspectorOpen && closeInspector()"
  >
    <header class="agent-workspace-header">
      <button
        class="quiet"
        :disabled="busy || changingContext"
        @click="emit('back')"
        aria-label="返回角色卡表单"
      >
        ←
      </button>
      <div class="agent-workspace-title">
        <span>CARD EDITOR <small>编辑工作区</small></span>
        <h1>{{ cardName }}</h1>
      </div>
      <span class="agent-draft-badge">{{ busy ? '编辑中' : '独立草稿' }}</span>
      <button
        v-if="narrow"
        ref="inspectorToggle"
        class="quiet"
        :aria-expanded="inspectorOpen"
        aria-controls="card-agent-inspector"
        @click="inspectorOpen ? closeInspector() : openInspector()"
      >
        角色资料
      </button>
    </header>
    <div class="agent-context-bar" aria-label="编辑对话管理">
      <label class="agent-context-choice"
        ><span>编辑对话</span
        ><select
          aria-label="切换编辑对话"
          :value="workspace.id"
          :disabled="contextControlsDisabled"
          @focus="refreshContexts"
          @change="chooseContext(($event.target as HTMLSelectElement).value)"
        >
          <option v-if="!contexts.some((context: any) => context.id === workspace.id)" :value="workspace.id">
            当前对话
          </option>
          <option v-for="context in contexts" :key="context.id" :value="context.id">
            {{ contextLabel(context) }}
          </option>
        </select></label
      >
      <button
        :disabled="contextControlsDisabled || !workspace.source_exists"
        title="从已保存的角色卡开始，不携带任何编辑对话历史"
        @click="newContext(false)"
      >
        ＋ 新建对话
      </button>
      <button
        class="quiet"
        :disabled="contextControlsDisabled"
        title="复制当前已完成的草稿，使用全新的聊天上下文；旧对话不变"
        @click="newContext(true)"
      >
        沿用草稿开新对话
      </button>
      <button
        class="quiet"
        :disabled="busy || changingContext"
        title="删除当前编辑对话及其草稿；运行中请先停止任务"
        @click="requestDelete"
      >
        删除编辑对话
      </button>
    </div>
    <div class="agent-workbench">
      <div class="agent-main" :inert="narrow && inspectorOpen">
        <div class="agent-thread-wrap">
          <div
            ref="conversation"
            class="agent-conversation"
            aria-label="编辑对话与动作"
            @scroll="trackScroll"
          >
            <div v-if="!runs.length" class="agent-welcome">
              <div class="agent-mark">◇</div>
              <h2>一起打磨这张角色卡</h2>
              <p>
                这是独立的新上下文，不携带其他编辑对话的历史。描述要改什么，Agent 会查阅资料并修改这份草稿。
              </p>
              <button :disabled="busy || changingContext" aria-label="适配本项目工作流" @click="send(true)">
                适配本项目工作流 ↗
              </button>
            </div>
            <section v-for="run in runs" :key="run.id" class="agent-run">
              <article class="editor-chat-message editor-user">
                <div class="editor-avatar">你</div>
                <div class="editor-message-body">
                  <div class="editor-message-meta">编辑要求</div>
                  <p>{{ run.request.text }}</p>
                </div>
              </article>
              <article class="editor-chat-message editor-assistant">
                <div class="editor-avatar">◇</div>
                <div class="editor-message-body">
                  <div class="editor-message-meta">
                    <strong>Card Editor</strong
                    ><span>{{
                      active?.id === run.id
                        ? '处理中'
                        : run.status === 'completed'
                          ? '已完成'
                          : actionStatus(run.status)
                    }}</span>
                  </div>
                  <div class="agent-actions" aria-label="Agent 动作">
                    <template v-for="step in actionsFor(run)" :key="step.id">
                      <div v-if="step.status === 'requested'" class="agent-model-step">
                        <span>◌</span>{{ step.title }}<small>{{ step.target }}</small>
                      </div>
                      <details
                        v-else
                        class="agent-tool-step"
                        :data-status="step.status"
                        :data-action-id="step.id"
                      >
                        <summary>
                          <span class="tool-status-icon">{{
                            step.status === 'completed' ? '✓' : step.status === 'running' ? '◌' : '!'
                          }}</span
                          ><span class="tool-title"
                            >{{ step.title }}<small v-if="step.target">{{ step.target }}</small></span
                          ><span class="tool-status">{{ actionStatus(step.status) }}</span>
                        </summary>
                        <div class="tool-detail">
                          <strong>输入</strong>
                          <pre>{{ step.arguments || '无参数' }}</pre>
                          <template v-if="step.result"
                            ><strong>结果</strong>
                            <pre>{{ step.result }}</pre>
                          </template>
                          <p v-if="step.truncated">内容较长，此处展示前 4000 字符。</p>
                          <small v-if="step.duration !== undefined">耗时 {{ step.duration }} ms</small>
                        </div>
                      </details>
                    </template>
                    <p v-if="activityErrors[run.id]" class="agent-inline-error">
                      动作加载失败 <button @click="hydrate(run)">重试</button>
                    </p>
                    <p
                      v-else-if="!actionsFor(run).length && active?.id !== run.id"
                      class="agent-activity-empty"
                    >
                      此记录没有可用的动作详情，可能来自早期版本或已清理。
                    </p>
                  </div>
                  <StoryProse
                    v-if="active?.id === run.id && partial"
                    :text="partial"
                    class="editor-response"
                  />
                  <StoryProse v-else-if="run.response" :text="run.response" class="editor-response" />
                  <p v-if="run.error" class="agent-inline-error">{{ run.error }}</p>
                  <p
                    v-if="['error', 'cancelled', 'interrupted'].includes(run.status)"
                    class="agent-activity-empty"
                  >
                    本轮未应用到草稿，保留上次完成的版本。
                  </p>
                  <p v-if="active?.id === run.id && !partial" class="agent-working" role="status">
                    {{ needsReload ? '编辑已结束，等待重新载入结果。' : '正在处理编辑要求…' }}
                  </p>
                </div>
              </article>
            </section>
          </div>
          <button v-if="!following" class="jump-edit-latest" @click="scrollToLatest(true)">↓ 最新动作</button>
        </div>
        <div class="agent-composer">
          <p v-if="error" role="alert" class="agent-inline-error">{{ error }}</p>
          <p v-if="reconnecting" class="agent-connection" role="status">
            连接中断，正在重新连接；编辑任务仍在后台运行。
          </p>
          <button v-if="needsReload" @click="refreshAfterRun">重新载入结果</button>
          <div class="agent-compose-box">
            <textarea
              v-model="input"
              aria-label="编辑要求"
              :disabled="busy || changingContext"
              rows="3"
              placeholder="描述你希望修改的内容…"
              @keydown.enter.exact="!$event.isComposing && ($event.preventDefault(), send())"
            />
            <div class="agent-compose-controls">
              <button class="quiet" :disabled="busy || changingContext" @click="send(true)">
                ◇ 适配工作流</button
              ><span>只修改草稿</span
              ><button
                v-if="active && !needsReload"
                @click="
                  guarded(async () => {
                    await api(`/card-edit-runs/${active.id}/cancel`, {});
                  })
                "
              >
                ■ 停止编辑</button
              ><button
                v-else
                class="primary"
                :disabled="busy || changingContext || !input.trim()"
                @click="send()"
              >
                发送编辑要求 ↑
              </button>
            </div>
          </div>
          <p class="agent-composer-hint">Enter 发送 · Shift + Enter 换行 · 在右侧审阅并保存</p>
        </div>
      </div>
      <button
        v-if="narrow && inspectorOpen"
        class="agent-inspector-backdrop"
        aria-label="关闭角色资料"
        tabindex="-1"
        @click="closeInspector"
      />
      <aside
        v-show="!narrow || inspectorOpen"
        id="card-agent-inspector"
        class="agent-inspector"
        :class="{ floating: narrow }"
        aria-label="编辑工作区资料"
      >
        <div class="agent-inspector-heading">
          <span>工作区资料</span
          ><button v-if="narrow" class="quiet" @click="closeInspector" aria-label="收起角色资料">×</button>
        </div>
        <div class="agent-inspector-tabs" role="tablist" aria-label="编辑工作区标签">
          <button
            v-for="tab in tabs"
            :id="`editor-tab-${tab.id}`"
            :key="tab.id"
            role="tab"
            :aria-selected="inspectorTab === tab.id"
            :aria-controls="`editor-panel-${tab.id}`"
            :tabindex="inspectorTab === tab.id ? 0 : -1"
            @click="inspectorTab = tab.id"
            @keydown.right.prevent="moveTab($event, 1)"
            @keydown.left.prevent="moveTab($event, -1)"
          >
            {{ tab.label }}<small v-if="tab.count">{{ tab.count }}</small>
          </button>
        </div>
        <section
          v-show="inspectorTab === 'data'"
          id="editor-panel-data"
          role="tabpanel"
          tabindex="0"
          aria-labelledby="editor-tab-data"
          class="agent-inspector-content"
        >
          <div class="inspector-section-heading">
            <h2>角色卡草稿</h2>
            <button class="quiet" @click="treeVersion++">全部折叠</button>
          </div>
          <p class="inspector-caption">
            {{ active ? '正在编辑；这里展示上次完成的草稿。' : '只读数据树，展开字段查看完整内容。' }}
          </p>
          <JsonDataTree :key="treeVersion" :value="workspace.document" />
        </section>
        <section
          v-show="inspectorTab === 'diagnostics'"
          id="editor-panel-diagnostics"
          role="tabpanel"
          tabindex="0"
          aria-labelledby="editor-tab-diagnostics"
          class="agent-inspector-content"
        >
          <h2>草稿诊断</h2>
          <div class="agent-check" :class="{ invalid: !validation.valid }">
            <span>{{ validation.valid ? '✓' : '!' }}</span
            >{{ validation.valid ? '结构检查通过' : '结构检查未通过' }}
          </div>
          <p>{{ validation.adapted ? '静态适配检查通过' : '尚未完成适配或仍需检查' }}</p>
          <ul v-if="validation.errors.length || validation.warnings.length" class="agent-diagnostics">
            <li v-for="(item, i) in [...validation.errors, ...validation.warnings]" :key="i">{{ item }}</li>
          </ul>
          <p v-else class="inspector-caption">当前静态检查未发现问题。</p>
          <p class="inspector-caption">静态检查不代表所有脚本都已运行验证。</p>
          <h2 v-if="toolFailures.length">编辑过程中的工具异常</h2>
          <details v-for="step in toolFailures" :key="step.id" class="agent-diff">
            <summary>
              {{ step.title }}<small>{{ step.target }}</small>
            </summary>
            <pre>{{ step.result }}</pre>
          </details>
          <p v-if="!workspace.source_exists" class="agent-inline-error">
            原卡已删除，草稿与资源仍保留，可另存副本。
          </p>
          <details v-for="run in runs.filter((item: any) => item.error)" :key="run.id">
            <summary>任务{{ actionStatus(run.status) }}</summary>
            <p>{{ run.error }}</p>
          </details>
        </section>
        <section
          v-show="inspectorTab === 'changes'"
          id="editor-panel-changes"
          role="tabpanel"
          tabindex="0"
          aria-labelledby="editor-tab-changes"
          class="agent-inspector-content agent-change-panel"
        >
          <div class="agent-save-actions">
            <p class="inspector-caption">保存只影响角色卡，已有对话继续使用原设定。</p>
            <button
              class="primary"
              :disabled="busy || changingContext || !validation.valid || !workspace.source_exists"
              @click="action('overwrite')"
            >
              覆盖保存原卡</button
            ><button :disabled="busy || changingContext || !validation.valid" @click="action('copy')">
              另存副本</button
            ><button :disabled="busy || changingContext || !workspace.can_undo" @click="action('undo')">
              撤销上一轮修改</button
            ><button
              :disabled="busy || changingContext || !workspace.source_exists"
              @click="action('reload')"
            >
              重新载入原卡</button
            ><button class="quiet" :disabled="busy || changingContext" @click="requestDelete">
              删除当前对话
            </button>
          </div>
          <details class="agent-changes-disclosure">
            <summary>
              与原卡的差异 <small>{{ changes.length }}</small>
            </summary>
            <div class="agent-changes-list">
              <p v-if="!changes.length" class="inspector-caption">暂无修改。</p>
              <details v-for="(change, i) in changes" :key="i" class="agent-diff">
                <summary>
                  {{ changeTitle(change.path) }}<small>{{ change.op }} · {{ change.path }}</small>
                </summary>
                <strong>修改前</strong>
                <pre>{{
                  JSON.stringify(pointerValue(workspace.base_document, change.path), null, 2) ?? '不存在'
                }}</pre>
                <strong>修改后</strong>
                <pre>{{
                  JSON.stringify(pointerValue(workspace.document, change.path), null, 2) ?? '不存在'
                }}</pre>
              </details>
            </div>
          </details>
        </section>
      </aside>
    </div>
  </section>
  <DeleteEditorDialog
    v-if="deleteDialog"
    :title="
      contextLabel(
        contexts.find((context) => context.id === workspace.id) || {
          id: workspace.id,
          title: '当前编辑对话',
        },
      )
    "
    :changes="changes.length"
    :has-input="!!input.trim()"
    :source-exists="workspace.source_exists"
    :can-save="validation.valid"
    :busy="deleteBusy"
    :error="deleteError"
    :background="dialogBackground"
    @close="deleteDialog = false"
    @remove="deleteContext"
  />
</template>
<style scoped>
.agent-editor {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  position: relative;
  background: var(--surface-base);
}
.agent-workspace-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px 22px;
  min-height: 78px;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}
.agent-workspace-title {
  flex: 1;
  min-width: 0;
}
.agent-workspace-title > span {
  color: var(--accent);
  font-size: 10px;
  letter-spacing: 0.12em;
}
.agent-workspace-title small {
  color: var(--text-muted);
  margin-left: 6px;
  letter-spacing: 0;
}
h1 {
  margin: 4px 0 0;
  font-size: 17px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.agent-draft-badge {
  padding: 4px 8px;
  border: 1px solid var(--border-control);
  border-radius: 6px;
  color: var(--text-muted);
  font-size: 11px;
  white-space: nowrap;
}
.agent-workbench {
  position: relative;
  display: flex;
  flex: 1;
  min-width: 0;
  min-height: 0;
}
.agent-main {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
  min-height: 0;
}
.agent-thread-wrap {
  flex: 1;
  min-height: 0;
  position: relative;
}
.agent-conversation {
  height: 100%;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 28px clamp(18px, 4vw, 48px);
  scroll-padding-bottom: 20px;
}
.agent-welcome {
  max-width: 520px;
  margin: 8vh auto;
  color: var(--text-secondary);
}
.agent-mark {
  width: 44px;
  height: 44px;
  display: grid;
  place-items: center;
  border-radius: 12px;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 25px;
  margin-bottom: 20px;
}
.agent-welcome h2 {
  font-size: 22px;
  color: var(--text-primary);
  font-weight: 500;
}
.agent-welcome p {
  line-height: 1.9;
  font-size: 13px;
  margin: 14px 0 24px;
}
.agent-run {
  max-width: 800px;
  margin: 0 auto 36px;
}
.editor-chat-message {
  display: flex;
  gap: 12px;
  margin-bottom: 26px;
  min-width: 0;
}
.editor-avatar {
  width: 29px;
  height: 29px;
  flex: 0 0 29px;
  display: grid;
  place-items: center;
  border-radius: 8px;
  font-size: 12px;
  background: var(--surface-hover);
  color: var(--text-secondary);
}
.editor-assistant .editor-avatar {
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 20px;
}
.editor-message-body {
  flex: 1;
  min-width: 0;
}
.editor-message-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 29px;
  font-size: 12px;
  color: var(--text-muted);
  margin-bottom: 9px;
}
.editor-message-meta strong {
  color: var(--text-primary);
  font-weight: 500;
}
.editor-message-meta span {
  font-size: 10px;
}
.editor-user p {
  margin: 0;
  padding: 12px 15px;
  border-radius: 0 10px 10px;
  background: var(--surface-panel);
  line-height: 1.8;
  font-size: 14px;
}
.agent-actions {
  margin-bottom: 16px;
}
.agent-model-step {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 0;
  color: var(--text-muted);
  font-size: 12px;
}
.agent-model-step small {
  margin-left: auto;
  font-size: 10px;
}
.agent-tool-step {
  padding: 0;
  margin: 6px 0;
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  background: var(--surface-input);
}
.agent-tool-step summary {
  display: flex;
  gap: 9px;
  align-items: center;
  list-style: none;
  padding: 10px 12px;
  font-size: 12px;
}
.agent-tool-step summary::after {
  content: '›';
  color: var(--text-muted);
  margin-left: 2px;
}
.agent-tool-step[open] summary::after {
  transform: rotate(90deg);
}
.tool-status-icon {
  color: var(--success);
}
[data-status='running'] .tool-status-icon {
  color: var(--accent);
  animation: tool-pulse 1.4s infinite alternate;
}
[data-status='error'] .tool-status-icon {
  color: var(--danger);
}
.tool-title {
  min-width: 0;
  flex: 1;
}
.tool-title small {
  display: block;
  color: var(--text-muted);
  font-size: 10px;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin-top: 3px;
}
.tool-status {
  font-size: 10px;
  color: var(--text-muted);
  flex-shrink: 0;
}
.tool-detail {
  padding: 12px;
  border-top: 1px solid var(--border-subtle);
  font-size: 11px;
}
.tool-detail pre,
.agent-diff pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 300px;
  overflow: auto;
  font-size: 11px;
}
.editor-response {
  font-size: 14px;
  line-height: 1.9;
}
p,
pre {
  overflow-wrap: anywhere;
}
.editor-user p {
  white-space: pre-wrap;
}
.agent-activity-empty,
.agent-working {
  font-size: 12px;
  color: var(--text-muted);
}
.agent-inline-error {
  color: var(--danger);
  font-size: 12px;
  white-space: pre-wrap;
  max-height: 100px;
  overflow: auto;
}
.agent-connection {
  color: var(--warning);
  font-size: 12px;
}
.agent-composer {
  padding: 12px clamp(18px, 4vw, 48px) 14px;
  flex-shrink: 0;
}
.agent-compose-box {
  max-width: 800px;
  margin: auto;
  border: 1px solid var(--border-strong);
  border-radius: 12px;
  background: var(--surface-panel);
  padding: 10px 12px;
}
.agent-compose-box:focus-within {
  border-color: var(--accent);
}
.agent-compose-box textarea {
  display: block;
  width: 100%;
  margin: 0;
  padding: 3px;
  min-height: 60px;
  max-height: min(160px, 22vh);
  resize: vertical;
  border: 0;
  background: transparent;
  font-size: 13px;
  box-shadow: none;
}
.agent-compose-box textarea:focus {
  outline: none;
}
.agent-compose-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}
.agent-compose-controls span {
  font-size: 10px;
  color: var(--text-muted);
  flex: 1;
}
.agent-compose-controls button {
  font-size: 11px;
  padding: 7px 10px;
}
.agent-composer-hint {
  max-width: 800px;
  margin: 8px auto 0;
  font-size: 10px;
  color: var(--text-faint);
}
.jump-edit-latest {
  position: absolute;
  bottom: 8px;
  right: 26px;
  border-radius: 18px;
  font-size: 11px;
  background: var(--surface-panel);
}
.agent-inspector {
  width: 330px;
  min-width: 280px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  border-left: 1px solid var(--border-subtle);
  background: var(--surface-sidebar);
  overflow: hidden;
}
.agent-inspector-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 18px 10px;
  font-size: 11px;
  color: var(--text-muted);
}
.agent-inspector-tabs {
  display: flex;
  gap: 4px;
  padding: 0 12px 12px;
  border-bottom: 1px solid var(--border-subtle);
}
.agent-inspector-tabs button {
  flex: 1;
  border: 0;
  padding: 8px;
  font-size: 12px;
  background: transparent;
  color: var(--text-muted);
  white-space: nowrap;
}
.agent-inspector-tabs button[aria-selected='true'] {
  color: var(--accent-hover);
  background: var(--accent-soft);
}
.agent-inspector-tabs small {
  margin-left: 5px;
  color: var(--text-secondary);
}
.agent-inspector-content {
  padding: 18px;
  min-height: 0;
  flex: 1;
  overflow: auto;
  overscroll-behavior: contain;
}
.agent-inspector-content h2 {
  font-size: 13px;
  margin: 0 0 16px;
  font-weight: 500;
}
.inspector-section-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}
.inspector-section-heading button {
  font-size: 10px;
  padding: 4px;
}
.inspector-caption {
  font-size: 11px;
  color: var(--text-muted);
  line-height: 1.8;
  margin: 0 0 15px;
}
.agent-check {
  display: flex;
  gap: 9px;
  padding: 12px;
  border: 1px solid var(--border-control);
  border-radius: 8px;
  color: var(--success);
  font-size: 12px;
}
.agent-check.invalid {
  color: var(--danger);
}
.agent-inspector-content > p {
  font-size: 12px;
  line-height: 1.8;
}
.agent-diagnostics {
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.9;
  color: var(--warning);
  overflow-wrap: anywhere;
}
.agent-diff {
  padding: 10px 0;
  border: 0;
  border-bottom: 1px solid var(--border-subtle);
  border-radius: 0;
}
.agent-diff summary {
  font-size: 12px;
  overflow-wrap: anywhere;
}
.agent-diff summary small {
  display: block;
  margin: 6px 0 0 13px;
  font-size: 10px;
  color: var(--text-muted);
}
.agent-diff > strong {
  display: block;
  font-size: 11px;
  margin-top: 14px;
}
.agent-save-actions {
  display: flex;
  flex-direction: column;
  gap: 9px;
}
.agent-save-actions button {
  font-size: 12px;
}
.agent-changes-disclosure {
  margin-top: 24px;
  padding: 0;
  border: 0;
}
.agent-changes-disclosure > summary {
  padding: 10px 0;
  border-top: 1px solid var(--border-subtle);
  border-bottom: 1px solid var(--border-subtle);
  font-size: 13px;
  font-weight: 500;
}
.agent-changes-disclosure > summary small {
  margin-left: 5px;
  color: var(--text-muted);
}
.agent-changes-list > .inspector-caption {
  margin-top: 15px;
}
.agent-inspector.floating {
  position: absolute;
  top: 0;
  bottom: 0;
  right: 0;
  z-index: 4;
  width: min(360px, 100%);
  min-width: 0;
  box-shadow: -12px 0 30px #0003;
}
.agent-inspector-backdrop {
  position: absolute;
  inset: 0;
  border: 0;
  border-radius: 0;
  z-index: 3;
  background: #0006;
}
@keyframes tool-pulse {
  to {
    opacity: 0.4;
  }
}
@media (prefers-reduced-motion: reduce) {
  [data-status='running'] .tool-status-icon {
    animation: none;
  }
}
@media (max-width: 620px) {
  .agent-workspace-header {
    padding: 12px 12px 12px 42px;
    gap: 8px;
  }
  .agent-draft-badge {
    display: none;
  }
  .agent-workspace-title small {
    display: none;
  }
  .agent-workspace-header button {
    font-size: 11px;
    padding: 6px;
  }
  .agent-conversation {
    padding: 20px 12px;
  }
  .editor-chat-message {
    gap: 8px;
  }
  .editor-avatar {
    width: 24px;
    height: 24px;
    flex-basis: 24px;
  }
  .agent-composer {
    padding: 10px 12px;
  }
  .agent-compose-controls span {
    display: none;
  }
  .agent-compose-controls {
    justify-content: space-between;
  }
  .agent-composer-hint {
    font-size: 9px;
  }
}
</style>

<style scoped>
.agent-context-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 9px 20px;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}
.agent-context-choice {
  display: flex;
  align-items: center;
  gap: 9px;
  flex: 1 1 210px;
  min-width: 0;
  margin: 0;
  font-size: 11px;
  color: var(--text-muted);
}
.agent-context-choice > span {
  flex-shrink: 0;
}
.agent-context-choice select {
  flex: 1;
  width: 0;
  min-width: 0;
  max-width: 360px;
  margin: 0;
  padding: 6px 28px 6px 9px;
  font-size: 11px;
  text-overflow: ellipsis;
}
.agent-context-bar button {
  margin: 0;
  font-size: 11px;
  padding: 6px 9px;
}
@media (max-width: 620px) {
  .agent-context-bar {
    padding: 8px 12px;
    gap: 6px;
  }
  .agent-context-choice {
    flex-basis: 100%;
  }
  .agent-context-choice select {
    max-width: none;
  }
}
</style>
