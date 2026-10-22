<script setup lang="ts">
import Icon from '../common/Icon.vue';
import CharacterPortrait from './CharacterPortrait.vue';
import CardSyncDialog from './CardSyncDialog.vue';
import CardDefaults from './CardDefaults.vue';
import WorldbookMountDialog from './WorldbookMountDialog.vue';
import { cardPortraitUrl, sessionPortraitUrl } from '../../lib/portraits';
import LoreBookEditor from './LoreBookEditor.vue';
import { emptyLoreFilter } from '../../lib/loreFilter';
import { useTimedError } from '../../composables/useTimedError';
import { computed, nextTick, onBeforeUnmount, ref, useId, watch } from 'vue';
import { api } from '../../lib/api';
import { appendRegexScripts } from '../../lib/regexScripts';
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
const regexFile = ref<HTMLInputElement>();
const loreEditor = ref<InstanceType<typeof LoreBookEditor>>();
const syncing = ref(false),
  mounting = ref(false);
const deletedGreetings = ref<{ index: number; text: string; cleared: boolean }[]>([]);
// Entry ID → library book it was mounted from, shown beside conversation entries.
const loreSources = computed<Record<string, string>>(() =>
  Object.fromEntries(
    (props.session?.snapshot.worldbooks || []).flatMap((item: any) =>
      item.entries.map((id: string) => [id, item.name]),
    ),
  ),
);
const entries = computed<any[]>(() => {
  const e = data.value.character_book?.entries || [];
  return Array.isArray(e) ? e : Object.values(e);
});
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
    loreEditor.value?.close();
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
async function importRegex(e: Event) {
  const input = e.target as HTMLInputElement;
  const files = Array.from(input.files || []);
  if (!files.length) return;
  await act(async () => {
    // Parse every file first so one invalid file adds nothing.
    const imported: unknown[] = [];
    for (const file of files) {
      try {
        imported.push(...[JSON.parse(await file.text())].flat());
      } catch {
        throw Error(`${file.name} 不是有效的 JSON 文件`);
      }
    }
    const scripts = appendRegexScripts(data.value.extensions?.regex_scripts, imported);
    (data.value.extensions ||= {}).regex_scripts = scripts;
    notice.value = `已添加 ${imported.length} 个正则脚本到草稿，保存后生效。`;
  });
  input.value = '';
}
function cardSynced(updated: any) {
  syncing.value = false;
  mounting.value = false;
  emit('sessionSaved', updated);
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
        <CardDefaults v-if="!props.session && !props.draftMode && !card._draftOnly" :card-id="card.id" />
        <button v-if="props.session" :disabled="busy" @click="mounting = true">
          <Icon name="book" :size="15" />挂载世界书
        </button>
        <button v-if="props.session" :disabled="busy" @click="syncing = true">
          <Icon name="refresh" :size="15" />从书架卡同步
        </button>
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
    <CardSyncDialog
      v-if="syncing && props.session"
      :session="props.session"
      :blocked="hasUnsavedChanges ? '编辑页有未保存修改，请先保存或恢复原稿状态后再同步。' : ''"
      @close="syncing = false"
      @synced="cardSynced"
    />
    <WorldbookMountDialog
      v-if="mounting && props.session"
      :session="props.session"
      :document="card.document"
      :blocked="hasUnsavedChanges ? '编辑页有未保存修改，请先保存或恢复原稿状态后再调整挂载。' : ''"
      @close="mounting = false"
      @mounted="cardSynced"
    />
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
        <template v-else-if="tab === 'lore'">
          <LoreBookEditor
            ref="loreEditor"
            v-model:book="data.character_book"
            :book-name="data.name"
            :filter="loreFilter"
            :library="!props.session"
            :sources="loreSources"
            save-hint="保存角色后生效"
            @notice="notice = $event"
            @error="error = $event"
          />
        </template>
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
              按匹配表达式处理显示文本或模型输入；状态面板的 HTML、CSS 和 JS 也可能在替换内容中。 导入的
              SillyTavern 正则 JSON 追加在现有脚本之后。
            </p>
            <div class="toolbar">
              <button :disabled="busy" @click="regexFile?.click()">
                <Icon name="import" :size="16" />导入正则 JSON
              </button>
              <input ref="regexFile" type="file" accept=".json" multiple hidden @change="importRegex" />
            </div>
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
