<script setup lang="ts">
import Icon from '../common/Icon.vue';
import CharacterPortrait from './CharacterPortrait.vue';
import { cardPortraitUrl, sessionPortraitUrl } from '../../lib/portraits';
import LoreFilter from './LoreFilter.vue';
import { audiences, entryAudience, emptyLoreFilter, filterLore } from '../../lib/loreFilter';
import FieldHelp from '../common/FieldHelp.vue';
import { useTimedError } from '../../composables/useTimedError';
import { computed, nextTick, onBeforeUnmount, ref, useId, watch } from 'vue';
import { api } from '../../lib/api';
import { appendLoreBook, loreFlagHelp, loreFlagValue, setLoreFlag } from '../../lib/loreBook';
import ExpandableTextarea from '../common/ExpandableTextarea.vue';
import { positionPopover } from '../../lib/anchoredPopover';
const clone = (value: any) => JSON.parse(JSON.stringify(value));
const props = defineProps<{ card: any; session?: any; draftMode?: boolean }>();
const sendEvent = defineEmits<{
  saved: [card: any];
  sessionSaved: [session: any];
  back: [];
  workspaceSelected: [id: string];
  openAgent: [id: string];
  draftSaved: [document: any];
}>();
let alive = true;
const emit: typeof sendEvent = (...args: any[]) => {
  if (alive) (sendEvent as any)(...args);
};
onBeforeUnmount(() => {
  alive = false;
  closeRestore();
  window.removeEventListener('resize', positionRestore);
  window.removeEventListener('scroll', positionRestore, true);
});
const card = ref(clone(props.card)),
  document = ref(clone(props.card.document));
const data = computed(() => document.value.data || document.value);
const workspace = ref<any>(null),
  agentRunning = ref(false);
const loreFilter = ref(emptyLoreFilter());
const tab = ref(props.session ? 'lore' : 'profile'),
  error = ref(''),
  notice = ref(''),
  busy = ref(false),
  raw = ref(JSON.stringify(document.value, null, 2));
const editorRoot = ref<HTMLElement>();
const restoreButton = ref<HTMLButtonElement>();
const restorePopover = ref<HTMLElement>();
const restoreConfirmation = ref<HTMLButtonElement>();
const confirmingRestore = ref(false);
const restorePopoverId = useId();
const loreFile = ref<HTMLInputElement>();
const loreDialog = ref<{ mode: 'add' | 'edit'; index: number; entry: any } | null>(null);
const confirmingLoreDelete = ref(false);
const deletedGreetings = ref<{ index: number; text: string; cleared: boolean }[]>([]);
const entries = computed<any[]>(() => {
  const e = data.value.character_book?.entries || [];
  return Array.isArray(e) ? e : Object.values(e);
});
const filteredLore = computed(() => filterLore(entries.value, loreFilter.value));
const loreGroups = computed(() => [
  {
    id: 'constant',
    title: '常驻条目',
    description: '每轮都注入上下文，不依赖触发词。',
    items: filteredLore.value.filter((x) => !!x.entry.constant),
  },
  {
    id: 'conditional',
    title: '条件触发条目',
    description: '通过关键词、概率、递归和扫描深度等条件激活。',
    items: filteredLore.value.filter((x) => !x.entry.constant),
  },
]);
const regexScripts = computed<any[]>(() =>
  Array.isArray(data.value.extensions?.regex_scripts) ? data.value.extensions.regex_scripts : [],
);
const scripts = computed(() => {
  const extensions = data.value.extensions || {};
  const rawHelper = extensions.tavern_helper;
  const helper = Array.isArray(rawHelper)
    ? Object.fromEntries(rawHelper.filter((part: any) => Array.isArray(part) && part.length === 2))
    : rawHelper || {};
  const rows: { script: any; path: string; key: string; inactive: boolean }[] = [];
  function collect(items: any, path: string, key: string, inactive: boolean) {
    if (!Array.isArray(items)) return;
    items.forEach((raw: any, index: number) => {
      const script = Array.isArray(raw) && raw.length === 2 ? raw[1] : raw;
      if (!script || typeof script !== 'object') return;
      const location = `${path} / ${script.name || script.id || index + 1}`;
      const locationKey = `${key}-${index}`;
      if (Array.isArray(script.scripts)) collect(script.scripts, location, locationKey, inactive);
      else rows.push({ script, path: location, key: locationKey, inactive });
    });
  }
  collect(helper.scripts, 'Tavern Helper', 'helper', false);
  collect(
    extensions.TavernHelper_scripts,
    '旧版 TavernHelper_scripts',
    'legacy',
    helper.scripts !== undefined,
  );
  return rows;
});
function validateScriptJson(text: string) {
  try {
    const value = JSON.parse(text);
    return value && typeof value === 'object' && !Array.isArray(value) ? '' : '脚本参数须为 JSON 对象。';
  } catch {
    return '脚本参数 JSON 格式无效。';
  }
}
function replaceScript(target: any, text: string) {
  const problem = validateScriptJson(text);
  if (problem) {
    error.value = problem;
    return;
  }
  const updated = JSON.parse(text);
  for (const key of Object.keys(target)) delete target[key];
  Object.assign(target, updated);
  error.value = '';
}
const draftKey = () => `tavern-card-draft-${props.session?.id || workspace.value?.id || card.value.id}`;
const hasUnsavedChanges = computed(
  () =>
    JSON.stringify(document.value) !== JSON.stringify(card.value.document) ||
    (tab.value === 'raw' && raw.value !== JSON.stringify(card.value.document, null, 2)),
);
try {
  const saved = sessionStorage.getItem(draftKey());
  if (saved) {
    const restored = JSON.parse(saved);
    document.value = restored.document;
    raw.value = restored.raw;
    tab.value = restored.tab === 'agent' ? 'profile' : restored.tab;
    notice.value = '已恢复这个标签页中的未保存草稿。';
  }
} catch {
  /* A malformed local draft does not block the saved card. */
}
watch(
  [document, raw, tab],
  () => {
    if (
      JSON.stringify(document.value) !== JSON.stringify(workspace.value?.document || card.value.document) ||
      (tab.value === 'raw' &&
        raw.value !== JSON.stringify(workspace.value?.document || card.value.document, null, 2))
    )
      sessionStorage.setItem(
        draftKey(),
        JSON.stringify({
          document: document.value,
          raw: raw.value,
          tab: tab.value,
          workspace_revision: workspace.value?.revision,
        }),
      );
    else sessionStorage.removeItem(draftKey());
  },
  { deep: true },
);
async function act(fn: () => Promise<any>) {
  error.value = '';
  busy.value = true;
  try {
    return await fn();
  } catch (e: any) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
async function requestRestore() {
  if (busy.value || agentRunning.value || !hasUnsavedChanges.value || !restorePopover.value) return;
  if (confirmingRestore.value) {
    cancelRestore();
    return;
  }
  restorePopover.value.showPopover();
  confirmingRestore.value = true;
  await nextTick();
  positionRestore();
  restoreConfirmation.value?.focus({ preventScroll: true });
}
function positionRestore() {
  if (restorePopover.value?.matches(':popover-open') && restoreButton.value)
    positionPopover(restorePopover.value, restoreButton.value);
}
function closeRestore() {
  restorePopover.value?.hidePopover();
  confirmingRestore.value = false;
}
function cancelRestore() {
  closeRestore();
  restoreButton.value?.focus({ preventScroll: true });
}
function syncRestorePopover() {
  confirmingRestore.value = restorePopover.value?.matches(':popover-open') || false;
}
window.addEventListener('resize', positionRestore);
window.addEventListener('scroll', positionRestore, true);
async function restoreSaved() {
  await act(async () => {
    // Fetch before clearing the draft so a failed reload preserves all edits.
    const saved = props.session || props.draftMode ? card.value : await api(`/cards/${card.value.id}`);
    if (!alive) return;
    const previousKey = draftKey();
    card.value = clone(saved);
    document.value = clone(saved.document);
    raw.value = JSON.stringify(document.value, null, 2);
    // An Agent conversation keeps its own draft; this action only resets this form.
    workspace.value = null;
    sessionStorage.removeItem(previousKey);
    sessionStorage.removeItem(draftKey());
    deletedGreetings.value = [];
    closeLore();
    closeRestore();
    notice.value = props.draftMode
      ? '已放弃表单修改，恢复打开表单时的草稿。'
      : props.session
        ? '已放弃未保存修改，恢复对话世界书。'
        : '已放弃未保存修改，恢复已保存的角色卡。';
  });
}
async function save() {
  await act(async () => {
    if (tab.value === 'raw') document.value = JSON.parse(raw.value);
    const previousKey = draftKey();
    if (props.draftMode) {
      // The parent clears the local draft only after its save request succeeds.
      emit('draftSaved', clone(document.value));
      return;
    }
    if (props.session) {
      const updated = await api(
        `/sessions/${props.session.id}/lorebook`,
        {
          document: document.value,
          expected_revision: props.session.revision,
          operation_id: crypto.randomUUID(),
        },
        'PUT',
      );
      card.value.document = clone(document.value);
      raw.value = JSON.stringify(document.value, null, 2);
      sessionStorage.removeItem(previousKey);
      emit('sessionSaved', updated);
      return;
    }
    if (workspace.value) {
      await syncWorkspace();
      card.value = await api(`/card-edits/${workspace.value.id}/save`, {
        expected_revision: workspace.value.revision,
        mode: 'overwrite',
        operation_id: crypto.randomUUID(),
      });
      workspace.value = await api(`/card-edits/${workspace.value.id}`);
    } else
      card.value = await api(
        `/cards/${card.value.id}`,
        {
          document: document.value,
          expected_revision: card.value.revision,
        },
        'PUT',
      );
    sessionStorage.removeItem(previousKey);
    document.value = clone(card.value.document);
    raw.value = JSON.stringify(document.value, null, 2);
    deletedGreetings.value = [];
    notice.value = '角色卡已保存。';
    emit('saved', card.value);
  });
}
async function syncWorkspace() {
  const previousKey = draftKey();
  if (!workspace.value)
    workspace.value = await api(`/cards/${card.value.id}/edits`, {
      document: document.value,
      expected_revision: card.value.revision,
    });
  else
    workspace.value = await api(
      `/card-edits/${workspace.value.id}`,
      {
        document: document.value,
        expected_revision: workspace.value.revision,
      },
      'PUT',
    );
  if (previousKey !== draftKey()) sessionStorage.removeItem(previousKey);
  rememberWorkspace();
}
function rememberWorkspace() {
  if (!workspace.value) return;
  localStorage.setItem(`tavern-card-edit-active-${workspace.value.card}`, workspace.value.id);
  emit('workspaceSelected', workspace.value.id);
}
async function openAgent() {
  if (busy.value) return;
  await act(async () => {
    if (tab.value === 'raw') document.value = JSON.parse(raw.value);
    if (!agentRunning.value) await syncWorkspace();
    emit('openAgent', workspace.value.id);
  });
}
async function changeTab(next: string) {
  if (next === 'agent') {
    await openAgent();
    return;
  }
  if (agentRunning.value) return;
  if (tab.value === 'raw') {
    try {
      document.value = JSON.parse(raw.value);
      deletedGreetings.value = [];
    } catch {
      error.value = '原始 JSON 格式无效，请先修正。';
      return;
    }
  }
  if (next === 'raw') raw.value = JSON.stringify(document.value, null, 2);
  tab.value = next;
}
function deleteGreeting(index: number) {
  const alternatives = data.value.alternate_greetings || [];
  const text = index === 0 ? data.value.first_mes || '' : alternatives[index - 1];
  if (typeof text !== 'string') return;
  const cleared = index === 0 && alternatives.length === 0;
  deletedGreetings.value.push({ index, text, cleared });
  if (index === 0) data.value.first_mes = alternatives.length ? alternatives.shift() : '';
  else alternatives.splice(index - 1, 1);
  notice.value = cleared
    ? '已清空最后一个开场白，保存角色后生效。'
    : `已删除开场 ${index + 1}，其余开场顺延；保存角色后生效。`;
}
function undoDeleteGreeting() {
  const removed = deletedGreetings.value.pop();
  if (!removed) return;
  if (removed.cleared) data.value.first_mes = removed.text;
  else if (removed.index === 0) {
    (data.value.alternate_greetings ||= []).unshift(data.value.first_mes || '');
    data.value.first_mes = removed.text;
  } else (data.value.alternate_greetings ||= []).splice(removed.index - 1, 0, removed.text);
  notice.value = '已撤销删除开场白，保存角色后生效。';
}
function addGreeting() {
  (data.value.alternate_greetings ||= []).push('');
}
function setAudience(entry: any, value: string) {
  ((entry.extensions ||= {}).terrarium ||= {}).audience = value;
}
async function permission(enabled: boolean) {
  await act(async () => {
    card.value = await api(`/cards/${card.value.id}/permission`, { enabled }, 'PUT');
    emit('saved', card.value);
  });
}
async function dependencies() {
  await act(async () => {
    const result = await api(`/cards/${card.value.id}/dependencies`, {});
    notice.value = `已缓存 ${Object.keys(result.resources).length} 项资源。`;
  });
}
async function importLore(e: Event) {
  const input = e.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  await act(async () => {
    const book = JSON.parse(await file.text());
    data.value.character_book = appendLoreBook(data.value.character_book, book);
    notice.value = '世界书条目已添加到草稿，保存后生效。';
  });
  input.value = '';
}
function addLore() {
  loreDialog.value = {
    mode: 'add',
    index: entries.value.length,
    entry: {
      id: crypto.randomUUID(),
      keys: [],
      content: '',
      comment: '新条目',
      enabled: true,
      extensions: { position: 1, terrarium: { audience: 'writer' } },
    },
  };
  confirmingLoreDelete.value = false;
}
function openLore(item: { index: number; entry: any }) {
  loreDialog.value = { mode: 'edit', index: item.index, entry: clone(item.entry) };
  confirmingLoreDelete.value = false;
}
function closeLore() {
  loreDialog.value = null;
  confirmingLoreDelete.value = false;
}
function loreSource() {
  data.value.character_book ||= { name: data.value.name, entries: [] };
  return data.value.character_book.entries;
}
function confirmLore() {
  if (!loreDialog.value) return;
  let source = loreSource();
  if (loreDialog.value.mode === 'add') {
    if (!Array.isArray(source)) {
      source = Object.values(source || {});
      data.value.character_book.entries = source;
    }
    source.push(clone(loreDialog.value.entry));
  } else if (Array.isArray(source)) source[loreDialog.value.index] = clone(loreDialog.value.entry);
  else {
    const key = Object.keys(source)[loreDialog.value.index];
    if (key !== undefined) source[key] = clone(loreDialog.value.entry);
  }
  notice.value =
    loreDialog.value.mode === 'add'
      ? '已添加世界书条目，保存角色后生效。'
      : '已更新世界书条目，保存角色后生效。';
  closeLore();
}
function deleteLore() {
  if (!loreDialog.value || loreDialog.value.mode === 'add') return;
  const source = loreSource();
  if (Array.isArray(source)) source.splice(loreDialog.value.index, 1);
  else {
    const key = Object.keys(source)[loreDialog.value.index];
    if (key !== undefined) delete source[key];
  }
  notice.value = '已删除世界书条目，保存角色后生效。';
  closeLore();
}
function replaceLoreDialogEntry(text: string) {
  if (!loreDialog.value) return;
  try {
    loreDialog.value.entry = JSON.parse(text);
    error.value = '';
  } catch {
    error.value = '条目完整参数 JSON 格式无效。';
  }
}
function listValue(value: any) {
  return Array.isArray(value) ? value.join(', ') : String(value || '');
}
function setList(entry: any, field: string, alias: string, event: Event) {
  const value = (event.target as HTMLInputElement).value
    .split(/[,，]/)
    .map((x) => x.trim())
    .filter(Boolean);
  entry[field] = value;
  entry[alias] = value;
}
function positionValue(entry: any) {
  return entry.extensions?.position ?? entry.position ?? 0;
}
function setPosition(entry: any, event: Event) {
  (entry.extensions ||= {}).position = Number((event.target as HTMLInputElement).value);
}
function validateJson(text: string) {
  try {
    JSON.parse(text);
    return '';
  } catch {
    return 'JSON 格式无效，请先修正。';
  }
}
useTimedError(error);
</script>
<template>
  <section ref="editorRoot" class="editor-page">
    <div class="editor-header">
      <div class="character-header-main">
        <button class="quiet editor-back" @click="emit('back')">
          <Icon name="arrow-left" :size="16" />返回
        </button>
        <div class="character-heading">
          <CharacterPortrait
            :src="session ? sessionPortraitUrl(session) : cardPortraitUrl(card)"
            :name="data.name"
            size="card"
            fit="contain"
          />
          <div>
            <h1>{{ data.name || '角色编辑器' }}</h1>
            <p class="muted">保留所有未编辑的扩展字段<span v-if="hasUnsavedChanges"> · 有未保存修改</span></p>
          </div>
        </div>
      </div>
      <div class="toolbar character-editor-actions">
        <a
          v-if="!props.session && !props.draftMode && !card._draftOnly"
          class="editor-export"
          :href="`/api/cards/${card.id}/export`"
          ><Icon name="import" :size="16" />导出角色卡</a
        >
        <button
          ref="restoreButton"
          :disabled="busy || agentRunning || !hasUnsavedChanges"
          aria-haspopup="dialog"
          :aria-expanded="confirmingRestore"
          :aria-controls="restorePopoverId"
          @click="requestRestore"
        >
          <Icon name="refresh" :size="15" />恢复原稿状态
        </button>
        <button class="primary" :disabled="busy || agentRunning || card._draftOnly" @click="save">
          {{ props.draftMode ? '保存草稿' : props.session ? '保存对话世界书' : '保存角色' }}
        </button>
      </div>
    </div>
    <AppNotification :message="error" />
    <AppNotification :message="notice" />
    <div
      ref="restorePopover"
      :id="restorePopoverId"
      popover="auto"
      class="editor-restore-confirm"
      role="dialog"
      aria-label="恢复原稿状态"
      @toggle="syncRestorePopover"
      @keydown.esc.prevent.stop="cancelRestore"
      @click.stop
    >
      <strong>恢复原稿状态？</strong>
      <p>
        {{
          props.draftMode ? '放弃表单修改，恢复打开表单时的草稿。' : '放弃全部未保存修改，恢复已保存内容。'
        }}
        放弃的修改不会再自动恢复。
      </p>
      <div class="toolbar">
        <button ref="restoreConfirmation" class="danger" :disabled="busy" @click="restoreSaved">
          确认放弃修改
        </button>
        <button :disabled="busy" @click="cancelRestore">继续编辑</button>
      </div>
    </div>
    <div class="editor-tabs">
      <button
        v-for="t in [
          { id: 'profile', label: '角色设定' },
          { id: 'prompts', label: '开场与提示词' },
          { id: 'lore', label: '世界书 ' + entries.length },
          { id: 'scripts', label: '脚本与兼容' },
          { id: 'raw', label: '原始 JSON' },
          { id: 'agent', label: 'Agent 编辑' },
        ].filter((t) => (!props.session || t.id === 'lore') && (!props.draftMode || t.id !== 'agent'))"
        :disabled="busy || (agentRunning && t.id !== 'agent')"
        :class="{ active: tab === t.id }"
        @click="changeTab(t.id)"
      >
        {{ t.label }}
      </button>
    </div>
    <div class="editor-layout character-editor-layout">
      <div class="editor-fields">
        <template v-if="tab === 'profile'"
          ><label>角色名称<input v-model="data.name" /></label
          ><label>角色描述<ExpandableTextarea v-model="data.description" label="角色描述" /></label
          ><label>性格<ExpandableTextarea v-model="data.personality" label="性格" /></label
          ><label>场景<ExpandableTextarea v-model="data.scenario" label="场景" /></label
          ><label>作者备注<ExpandableTextarea v-model="data.creator_notes" label="作者备注" /></label
          ><label
            >标签（逗号分隔）<input
              :value="data.tags?.join(', ')"
              @change="
                data.tags = ($event.target as HTMLInputElement).value
                  .split(/[,，]/)
                  .map((x) => x.trim())
                  .filter(Boolean)
              " /></label
        ></template>
        <template v-else-if="tab === 'prompts'">
          <p class="muted">
            开场编号与进入故事时一致。删除后其余开场顺延，保存角色后生效；删除最后一个开场会清空内容。
          </p>
          <div>
            <label>开场 1（默认）<ExpandableTextarea v-model="data.first_mes" label="开场白" /></label
            ><button class="danger" :disabled="busy" @click="deleteGreeting(0)">删除开场 1</button>
          </div>
          <details v-for="(_, i) in data.alternate_greetings" :key="i">
            <summary>开场 {{ i + 2 }}</summary>
            <ExpandableTextarea v-model="data.alternate_greetings[i]" :label="'开场 ' + (i + 2)" /><button
              class="danger"
              :disabled="busy"
              @click="deleteGreeting(i + 1)"
            >
              删除开场 {{ i + 2 }}
            </button>
          </details>
          <div class="toolbar">
            <button :disabled="busy" @click="addGreeting"><Icon name="plus" :size="16" />添加候选开场</button
            ><button v-if="deletedGreetings.length" :disabled="busy" @click="undoDeleteGreeting">
              撤销删除开场白
            </button>
          </div>
          <label>示例对话<ExpandableTextarea v-model="data.mes_example" label="示例对话" /></label
          ><label>角色系统提示<ExpandableTextarea v-model="data.system_prompt" label="角色系统提示" /></label
          ><label
            >历史后提示<ExpandableTextarea v-model="data.post_history_instructions" label="历史后提示"
          /></label>
        </template>
        <template v-else-if="tab === 'lore'"
          ><p class="muted">
            随对话滚动方案下，非常驻条目统一作为 system
            紧贴用户消息前注入，首次有开场白时放在开场白前；重复命中不移动。位置、插入深度和角色字段只对常驻与
            ST 方案生效。持续轮数不再参与运行，原始兼容字段仍保留在 JSON 中。
            列表尽量按同批注入顺序显示，不改变源数据顺序；实际注入仍受触发条件和上下文方案影响。
          </p>
          <div class="toolbar">
            <button @click="addLore"><Icon name="plus" :size="16" />添加条目</button
            ><button @click="loreFile?.click()"><Icon name="import" :size="16" />导入世界书 JSON</button
            ><input ref="loreFile" type="file" accept=".json" hidden @change="importLore" />
          </div>
          <LoreFilter :filter="loreFilter" :count="filteredLore.length" :total="entries.length" />
          <section v-for="group in loreGroups" :key="group.id" class="lore-group">
            <div class="lore-group-heading">
              <div>
                <h3>
                  {{ group.title }} <span>{{ group.items.length }}</span>
                </h3>
                <p>{{ group.description }}</p>
              </div>
            </div>
            <p v-if="!group.items.length" class="empty-small">暂无{{ group.title }}。</p>
            <div class="lore-entry-grid">
              <details
                v-for="item in group.items"
                :key="item.entry.id ?? item.entry.uid ?? item.index"
                class="lore-editor"
                :class="{ 'mvu-lore-entry': /\[initvar\]/i.test(item.entry.comment || '') }"
                :data-lore-id="String(item.entry.id ?? item.entry.uid ?? item.index)"
                @click.prevent="openLore(item)"
              >
                <summary>
                  {{ item.entry.comment || item.entry.name || '条目 ' + (item.index + 1) }}
                  <small>{{ audiences[entryAudience(item.entry)] }}</small>
                  <small>{{ item.entry.enabled === false || item.entry.disable ? '未启用' : '' }}</small>
                  <span
                    v-if="/\[initvar\]/i.test(item.entry.comment || '')"
                    class="mvu-entry-label"
                    title="[InitVar] 无论启停与用途均参与初始化；标记 tracker_mvu 后也向变量任务提供完整正文。"
                  >
                    已识别 MVU 条目
                  </span>
                </summary>
              </details>
            </div>
          </section>
          <Teleport to="body">
            <div v-if="loreDialog" class="modal-backdrop lore-modal-backdrop">
              <section
                class="modal wide modal-large lore-entry-modal"
                role="dialog"
                aria-modal="true"
                :aria-label="loreDialog.mode === 'add' ? '添加世界书条目' : '编辑世界书条目'"
                @click.stop
                @keydown.esc.prevent
              >
                <header class="lore-modal-header">
                  <div>
                    <span class="eyebrow">世界书条目</span>
                    <h2>
                      {{
                        loreDialog.mode === 'add'
                          ? '添加条目'
                          : loreDialog.entry.comment || loreDialog.entry.name || '编辑条目'
                      }}
                    </h2>
                  </div>
                </header>
                <div class="modal-scroll lore-modal-form">
                  <div class="lore-form-grid">
                    <label
                      >条目 ID <FieldHelp tooltip="世界书条目的稳定标识；脚本或外部工具可能依赖它。" /><input
                        v-model="loreDialog.entry.id" /></label
                    ><label>条目名称<input v-model="loreDialog.entry.comment" autofocus /></label>
                  </div>
                  <div class="lore-checks">
                    <label class="check"
                      ><input
                        type="checkbox"
                        :checked="loreDialog.entry.enabled !== false && !loreDialog.entry.disable"
                        @change="
                          loreDialog.entry.enabled = ($event.target as HTMLInputElement).checked;
                          loreDialog.entry.disable = !loreDialog.entry.enabled;
                        "
                      />启用</label
                    ><label class="check"
                      ><input type="checkbox" v-model="loreDialog.entry.constant" />常驻条目</label
                    ><label class="check"
                      ><input
                        type="checkbox"
                        :checked="loreFlagValue(loreDialog.entry, 'caseSensitive')"
                        @change="
                          setLoreFlag(
                            loreDialog.entry,
                            'caseSensitive',
                            ($event.target as HTMLInputElement).checked,
                          )
                        " />区分大小写<FieldHelp
                        :tooltip="loreFlagHelp(loreDialog.entry, 'caseSensitive')" /></label
                    ><label class="check"
                      ><input
                        type="checkbox"
                        :checked="loreFlagValue(loreDialog.entry, 'matchWholeWords')"
                        @change="
                          setLoreFlag(
                            loreDialog.entry,
                            'matchWholeWords',
                            ($event.target as HTMLInputElement).checked,
                          )
                        " />全词匹配<FieldHelp
                        :tooltip="loreFlagHelp(loreDialog.entry, 'matchWholeWords')" /></label
                    ><label class="check"
                      ><input type="checkbox" v-model="loreDialog.entry.use_regex" />正则触发</label
                    >
                  </div>
                  <label
                    >主触发词（逗号分隔）<input
                      :value="listValue(loreDialog.entry.keys ?? loreDialog.entry.key)"
                      @change="setList(loreDialog.entry, 'keys', 'key', $event)" /></label
                  ><label
                    >辅助触发词（逗号分隔）
                    <FieldHelp
                      tooltip="与主触发词一起参与选择逻辑；是否需要任一、全部或排除，由“辅助词逻辑”决定。" /><input
                      :value="listValue(loreDialog.entry.secondary_keys ?? loreDialog.entry.keysecondary)"
                      @change="setList(loreDialog.entry, 'secondary_keys', 'keysecondary', $event)" /></label
                  ><label
                    >内容<ExpandableTextarea v-model="loreDialog.entry.content" label="世界书内容"
                  /></label>
                  <div class="lore-form-grid">
                    <label
                      >插入顺序
                      <FieldHelp tooltip="数值越小通常越早参与排序；相同位置的条目按此值决定先后。" /><input
                        type="number"
                        v-model.number="loreDialog.entry.insertion_order" /></label
                    ><label
                      >插入位置 <FieldHelp tooltip="滚动方案下仅常驻使用；ST 方案按此位置注入。" /><input
                        type="number"
                        :value="positionValue(loreDialog.entry)"
                        @input="setPosition(loreDialog.entry, $event)" /></label
                    ><label
                      >插入深度
                      <FieldHelp
                        tooltip="相对最近历史消息向前插入的层数；0 表示放在当前上下文末端附近。" /><input
                        type="number"
                        min="0"
                        v-model.number="loreDialog.entry.depth" /></label
                    ><label
                      >扫描深度
                      <FieldHelp tooltip="从最近多少条历史消息中搜索触发词；0 通常只检查本轮输入。" /><input
                        type="number"
                        min="0"
                        v-model.number="loreDialog.entry.scan_depth" /></label
                    ><label
                      >触发概率 %
                      <FieldHelp tooltip="关键词命中后仍需通过的概率；关闭“使用概率”时按 100% 处理。" /><input
                        type="number"
                        min="0"
                        max="100"
                        v-model.number="loreDialog.entry.probability" /></label
                    ><label
                      >辅助词逻辑
                      <FieldHelp
                        tooltip="0：任一命中；1：并非全部命中；2：全部不命中；3：全部命中。"
                      /><select v-model.number="loreDialog.entry.selectiveLogic">
                        <option :value="0">任一命中</option>
                        <option :value="1">并非全部</option>
                        <option :value="2">全部不命中</option>
                        <option :value="3">全部命中</option>
                      </select></label
                    ><label
                      >恢复尺度（轮）
                      <FieldHelp tooltip="条目离开实际正文请求后才开始计时并逐渐提高再次触发机会。" /><input
                        type="number"
                        min="0"
                        v-model.number="loreDialog.entry.cooldown" /></label
                    ><label
                      >延迟轮数
                      <FieldHelp tooltip="部分角色卡前端用于推迟条目生效；保留并展示该兼容参数。" /><input
                        type="number"
                        min="0"
                        v-model.number="loreDialog.entry.delay" /></label
                    ><label
                      >消息角色
                      <FieldHelp tooltip="条目注入模型上下文时采用的消息角色；数字枚举会原样保留。" /><input
                        v-model="loreDialog.entry.role"
                    /></label>
                  </div>
                  <div class="lore-checks">
                    <label class="check"
                      ><input type="checkbox" v-model="loreDialog.entry.useProbability" />使用概率</label
                    ><label class="check"
                      ><input
                        type="checkbox"
                        :checked="loreFlagValue(loreDialog.entry, 'preventRecursion')"
                        @change="
                          setLoreFlag(
                            loreDialog.entry,
                            'preventRecursion',
                            ($event.target as HTMLInputElement).checked,
                          )
                        " />阻止递归<FieldHelp
                        :tooltip="loreFlagHelp(loreDialog.entry, 'preventRecursion')" /></label
                    ><label class="check"
                      ><input
                        type="checkbox"
                        :checked="loreFlagValue(loreDialog.entry, 'excludeRecursion')"
                        @change="
                          setLoreFlag(
                            loreDialog.entry,
                            'excludeRecursion',
                            ($event.target as HTMLInputElement).checked,
                          )
                        " />不参与递归扫描<FieldHelp
                        :tooltip="loreFlagHelp(loreDialog.entry, 'excludeRecursion')" /></label
                    ><label class="check"
                      ><input type="checkbox" v-model="loreDialog.entry.group_override" />覆盖同组条目</label
                    >
                  </div>
                  <div class="lore-form-grid">
                    <label>分组<input v-model="loreDialog.entry.group" /></label
                    ><label
                      >分组权重<input type="number" v-model.number="loreDialog.entry.group_weight"
                    /></label>
                  </div>
                  <details class="lore-all-params">
                    <summary>
                      全部参数（JSON）
                      <FieldHelp
                        tooltip="这里完整展示该条目的原始对象，包括当前表单未单独解释的扩展字段。修改后离开文本框即可应用。"
                      />
                    </summary>
                    <ExpandableTextarea
                      :model-value="JSON.stringify(loreDialog.entry, null, 2)"
                      label="世界书全部参数（JSON）"
                      code
                      :validate="validateJson"
                      @change="replaceLoreDialogEntry($event)"
                    />
                  </details>
                  <label class="lore-audience-field"
                    >条目用途<select
                      :value="entryAudience(loreDialog.entry)"
                      @change="setAudience(loreDialog.entry, ($event.target as HTMLSelectElement).value)"
                    >
                      <option v-for="(label, key) in audiences" :key="key" :value="key">{{ label }}</option>
                    </select></label
                  >
                </div>
                <footer class="modal-footer lore-modal-footer">
                  <div class="lore-delete-zone" v-if="loreDialog.mode === 'edit'">
                    <template v-if="confirmingLoreDelete">
                      <span>确定删除此条目？</span>
                      <button class="danger" @click="deleteLore">确认删除</button>
                      <button @click="confirmingLoreDelete = false">保留条目</button>
                    </template>
                    <button v-else class="danger" @click="confirmingLoreDelete = true">删除条目</button>
                  </div>
                  <div class="lore-modal-actions">
                    <button @click="closeLore">取消</button>
                    <button class="primary" @click="confirmLore">确认</button>
                  </div>
                </footer>
              </section>
            </div>
          </Teleport></template
        >
        <template v-else-if="tab === 'scripts'">
          <label class="check">
            <input
              type="checkbox"
              :checked="card.permission"
              :disabled="card._draftOnly || props.draftMode"
              @change="permission(($event.target as HTMLInputElement).checked)"
            />允许此卡在隔离环境中执行脚本
          </label>
          <button
            :disabled="busy || !card.permission || card._draftOnly || props.draftMode"
            @click="dependencies"
          >
            检查并缓存外部依赖
          </button>
          <section class="card-script-group">
            <h3>
              正则脚本 <small>{{ regexScripts.length }}</small>
            </h3>
            <p class="muted">
              按匹配表达式处理显示文本或模型输入；状态面板的 HTML、CSS 和 JS 也可能在替换内容中。
            </p>
            <p v-if="!regexScripts.length" class="muted">这张卡没有正则脚本。</p>
            <details v-for="(script, i) in regexScripts" :key="`regex-${i}`" class="card-script-entry">
              <summary>
                {{ script.scriptName || script.name || '正则脚本 ' + (i + 1)
                }}<small v-if="script.disabled">未启用</small>
              </summary>
              <label>名称<input v-model="script.scriptName" /></label>
              <label class="check"
                ><input
                  type="checkbox"
                  :checked="!script.disabled"
                  @change="script.disabled = !($event.target as HTMLInputElement).checked"
                />启用</label
              >
              <label
                >匹配表达式<ExpandableTextarea
                  v-model="script.findRegex"
                  :label="`匹配表达式：${script.scriptName || i + 1}`"
                  code
              /></label>
              <label
                >替换内容<ExpandableTextarea
                  v-model="script.replaceString"
                  :label="`替换内容：${script.scriptName || i + 1}`"
                  code
              /></label>
              <div class="card-script-options">
                <label class="check"><input type="checkbox" v-model="script.markdownOnly" />显示阶段</label>
                <label class="check"><input type="checkbox" v-model="script.promptOnly" />模型输入阶段</label>
                <span
                  >应用位置：{{
                    Array.isArray(script.placement) ? script.placement.join(', ') : '默认'
                  }}</span
                >
              </div>
              <p class="muted">阶段选项只勾一项时限制为该阶段；都不勾或都勾时两个阶段均可执行。</p>
              <details class="card-script-raw">
                <summary>全部参数（JSON）</summary>
                <ExpandableTextarea
                  :model-value="JSON.stringify(script, null, 2)"
                  :label="`正则脚本全部参数：${script.scriptName || i + 1}`"
                  code
                  :validate="validateScriptJson"
                  @change="replaceScript(script, $event)"
                />
              </details>
            </details>
          </section>
          <section class="card-script-group">
            <h3>
              角色脚本 <small>{{ scripts.length }}</small>
            </h3>
            <p class="muted">
              包含 Tavern Helper 的分组脚本与旧版脚本字段；旧版字段若被新版覆盖，仍可在此查看和编辑。
            </p>
            <p v-if="!scripts.length" class="muted">这张卡没有角色脚本。</p>
            <details v-for="(item, i) in scripts" :key="item.key" class="card-script-entry">
              <summary>
                {{ item.script.name || '脚本 ' + (i + 1) }}<small v-if="item.inactive">旧字段未执行</small
                ><small v-else-if="item.script.enabled === false">未启用</small>
              </summary>
              <p class="muted">{{ item.path }}</p>
              <label>名称<input v-model="item.script.name" /></label>
              <label class="check"
                ><input
                  type="checkbox"
                  :checked="item.script.enabled !== false"
                  @change="item.script.enabled = ($event.target as HTMLInputElement).checked"
                />启用</label
              >
              <label
                >脚本内容<ExpandableTextarea
                  v-model="item.script.content"
                  :label="item.script.name || '脚本 ' + (i + 1)"
                  code
              /></label>
              <details class="card-script-raw">
                <summary>全部参数（JSON）</summary>
                <ExpandableTextarea
                  :model-value="JSON.stringify(item.script, null, 2)"
                  :label="`角色脚本全部参数：${item.script.name || i + 1}`"
                  code
                  :validate="validateScriptJson"
                  @change="replaceScript(item.script, $event)"
                />
              </details>
            </details>
          </section>
          <details class="card-script-diagnostics">
            <summary>兼容性检查</summary>
            <pre>{{ JSON.stringify(card.report, null, 2) }}</pre>
          </details>
        </template>
        <template v-else
          ><p class="muted">用于编辑复杂扩展与未在表单中展示的字段。</p>
          <ExpandableTextarea v-model="raw" label="原始 JSON" code :validate="validateJson"
        /></template>
      </div>
    </div>
  </section>
</template>
