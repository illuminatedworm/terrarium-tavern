<script setup lang="ts">
import Icon from '../common/Icon.vue';
import FieldHelp from '../common/FieldHelp.vue';
import ExpandableTextarea from '../common/ExpandableTextarea.vue';
import LoreFilter from './LoreFilter.vue';
import WorldbookPicker from './WorldbookPicker.vue';
import {
  audiences,
  entryAudience,
  filterLore,
  type LoreFilter as LoreFilterState,
} from '../../lib/loreFilter';
import {
  appendLoreBook,
  isInitVar,
  loreFlagHelp,
  loreField,
  loreFieldHelp,
  loreFlagValue,
  loreTitle,
  setLoreField,
  setLoreFlag,
} from '../../lib/loreBook';
import { positionPopover } from '../../lib/anchoredPopover';
import { computed, nextTick, onBeforeUnmount, ref, useId } from 'vue';
import type { Worldbook } from '../../lib/types';

/**
 * Entry list and entry dialog for one worldbook object (a card's `character_book`
 * or a library book). Edits mutate `book` in place like the rest of the card
 * draft; a missing book is created and handed back through `update:book`.
 */
const clone = (value: any) => JSON.parse(JSON.stringify(value));
const props = defineProps<{
  book: any;
  /** Name given to a book this editor has to create. */
  bookName: string;
  filter: LoreFilterState;
  /** Shown after each draft change, e.g. “保存角色后生效”. */
  saveHint: string;
  /** Offer importing from the worldbook library besides JSON files. */
  library?: boolean;
  /** Entry ID → name of the library book it was mounted from. */
  sources?: Record<string, string>;
}>();
const emit = defineEmits<{
  'update:book': [book: any];
  notice: [message: string];
  error: [message: string];
}>();
const loreFile = ref<HTMLInputElement>();
const importButton = ref<HTMLButtonElement>();
const importMenu = ref<HTMLElement>();
const importMenuId = useId();
const importOpen = ref(false);
const picking = ref(false);
const loreDialog = ref<{ mode: 'add' | 'edit'; index: number; entry: any } | null>(null);
const confirmingLoreDelete = ref(false);
const entries = computed<any[]>(() => {
  const e = props.book?.entries || [];
  return Array.isArray(e) ? e : Object.values(e);
});
const filteredLore = computed(() => filterLore(entries.value, props.filter));
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
function loreId(item: { index: number; entry: any }) {
  return String(item.entry.id ?? item.entry.uid ?? item.index);
}
function positionImport() {
  if (importMenu.value?.matches(':popover-open') && importButton.value)
    positionPopover(importMenu.value, importButton.value);
}
async function importToggled(event: Event) {
  importOpen.value = (event as ToggleEvent).newState === 'open';
  if (!importOpen.value) return;
  await nextTick();
  positionImport();
  importMenu.value?.querySelector<HTMLButtonElement>('button')?.focus({ preventScroll: true });
}
window.addEventListener('resize', positionImport);
window.addEventListener('scroll', positionImport, true);
onBeforeUnmount(() => {
  window.removeEventListener('resize', positionImport);
  window.removeEventListener('scroll', positionImport, true);
});
function chooseFile() {
  importMenu.value?.hidePopover();
  loreFile.value?.click();
}
function chooseLibrary() {
  importMenu.value?.hidePopover();
  picking.value = true;
}
async function importLore(e: Event) {
  const input = e.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = '';
  if (!file) return;
  try {
    emit('update:book', appendLoreBook(props.book, JSON.parse(await file.text())));
    emit('notice', '世界书条目已添加到草稿，保存后生效。');
  } catch (error: any) {
    emit('error', error.message);
  }
}
function importLibrary(picked: Worldbook) {
  picking.value = false;
  try {
    emit('update:book', appendLoreBook(props.book, picked.book));
    const count = Object.keys(picked.book.entries).length;
    emit('notice', `已从世界书库导入「${picked.name}」的 ${count} 个条目，保存后生效。`);
  } catch (error: any) {
    emit('error', error.message);
  }
  importButton.value?.focus({ preventScroll: true });
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
function loreBook() {
  if (props.book) return props.book;
  const book = { name: props.bookName, entries: [] };
  emit('update:book', book);
  return book;
}
function confirmLore() {
  if (!loreDialog.value) return;
  const book = loreBook();
  let source = book.entries;
  if (loreDialog.value.mode === 'add') {
    if (!Array.isArray(source)) {
      source = Object.values(source || {});
      book.entries = source;
    }
    source.push(clone(loreDialog.value.entry));
  } else if (Array.isArray(source)) source[loreDialog.value.index] = clone(loreDialog.value.entry);
  else {
    const key = Object.keys(source)[loreDialog.value.index];
    if (key !== undefined) source[key] = clone(loreDialog.value.entry);
  }
  emit(
    'notice',
    loreDialog.value.mode === 'add'
      ? `已添加世界书条目，${props.saveHint}。`
      : `已更新世界书条目，${props.saveHint}。`,
  );
  closeLore();
}
function deleteLore() {
  if (!loreDialog.value || loreDialog.value.mode === 'add') return;
  const source = loreBook().entries;
  if (Array.isArray(source)) source.splice(loreDialog.value.index, 1);
  else {
    const key = Object.keys(source)[loreDialog.value.index];
    if (key !== undefined) delete source[key];
  }
  emit('notice', `已删除世界书条目，${props.saveHint}。`);
  closeLore();
}
function replaceLoreDialogEntry(text: string) {
  if (!loreDialog.value) return;
  try {
    loreDialog.value.entry = JSON.parse(text);
    emit('error', '');
  } catch {
    emit('error', '条目完整参数 JSON 格式无效。');
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
// Description and runtime default of fields that may live at the top level, in
// extensions or under a legacy alias; the tip names the effective location.
const fieldNotes: Record<string, [string, string]> = {
  insertion_order: ['数值越小通常越早参与排序；相同位置的条目按此值决定先后。', '为 100'],
  depth: ['相对最近历史消息向前插入的层数；0 表示放在当前上下文末端附近。', '为 0'],
  scan_depth: ['从最近多少条历史消息中搜索触发词；0 通常只检查本轮输入。', '为 4'],
  probability: ['关键词命中后仍需通过的概率；关闭“使用概率”时按 100% 处理。', '为 100%'],
  selectiveLogic: ['0：任一命中；1：并非全部命中；2：全部不命中；3：全部命中。', '为“任一命中”'],
  cooldown: ['条目离开实际正文请求后才开始计时并逐渐提高再次触发机会。', '为 0'],
  delay: ['部分角色卡前端用于推迟条目生效；保留并展示该兼容参数。', ''],
  role: ['用于识别条目来源类型，数字枚举原样保留；最终发送角色以消息适配为准。', '为 system'],
  group: ['', ''],
  group_weight: ['', ''],
  useProbability: ['', '开启'],
};
function fieldTip(entry: any, field: string) {
  const [note, fallback] = fieldNotes[field];
  return [note, loreFieldHelp(entry, field, fallback)].filter(Boolean).join('');
}
function numberValue(event: Event) {
  // Same as v-model.number: an empty field stays an empty string.
  const value = (event.target as HTMLInputElement).value;
  return value === '' ? '' : Number(value);
}
function positionValue(entry: any) {
  return entry.extensions?.position ?? entry.position ?? 0;
}
function setPosition(entry: any, event: Event) {
  (entry.extensions ||= {}).position = Number((event.target as HTMLInputElement).value);
}
function setAudience(entry: any, value: string) {
  ((entry.extensions ||= {}).terrarium ||= {}).audience = value;
}
function validateJson(text: string) {
  try {
    JSON.parse(text);
    return '';
  } catch {
    return 'JSON 格式无效，请先修正。';
  }
}
defineExpose({ close: closeLore });
</script>

<template>
  <p class="muted">
    随对话滚动方案下，非常驻条目统一作为 system
    紧贴用户消息前注入，首次有开场白时放在开场白前；重复命中不移动。位置、插入深度和角色字段只对常驻与 ST
    方案生效。持续轮数不再参与运行，原始兼容字段仍保留在 JSON 中。
    列表尽量按同批注入顺序显示，不改变源数据顺序；实际注入仍受触发条件和上下文方案影响。
  </p>
  <div class="toolbar">
    <button @click="addLore"><Icon name="plus" :size="16" />添加条目</button>
    <button
      v-if="library"
      ref="importButton"
      :popovertarget="importMenuId"
      aria-haspopup="menu"
      :aria-expanded="importOpen"
    >
      <Icon name="import" :size="16" />导入世界书<Icon name="chevron-down" :size="14" />
    </button>
    <button v-else @click="loreFile?.click()"><Icon name="import" :size="16" />导入世界书 JSON</button>
    <input ref="loreFile" type="file" accept=".json" hidden @change="importLore" />
  </div>
  <div
    v-if="library"
    :id="importMenuId"
    ref="importMenu"
    popover="auto"
    class="lore-import-menu"
    role="menu"
    aria-label="导入世界书"
    @toggle="importToggled"
  >
    <button class="quiet" role="menuitem" @click="chooseFile">
      <Icon name="file" :size="15" />从文件导入 JSON
    </button>
    <button class="quiet" role="menuitem" @click="chooseLibrary">
      <Icon name="book" :size="15" />从世界书库导入…
    </button>
  </div>
  <WorldbookPicker v-if="picking" @close="picking = false" @picked="importLibrary" />
  <LoreFilter :filter="filter" :count="filteredLore.length" :total="entries.length" />
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
        :key="loreId(item)"
        class="lore-editor"
        :class="{ 'mvu-lore-entry': isInitVar(item.entry) }"
        :data-lore-id="loreId(item)"
        @click.prevent="openLore(item)"
      >
        <summary>
          {{ loreTitle(item.entry) || '条目 ' + (item.index + 1) }}
          <small>{{ audiences[entryAudience(item.entry)] }}</small>
          <small v-if="sources?.[loreId(item)]" class="lore-source-label"
            >来自「{{ sources[loreId(item)] }}」</small
          >
          <small>{{ item.entry.enabled === false || item.entry.disable ? '未启用' : '' }}</small>
          <span
            v-if="isInitVar(item.entry)"
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
              {{ loreDialog.mode === 'add' ? '添加条目' : loreTitle(loreDialog.entry) || '编辑条目' }}
            </h2>
          </div>
        </header>
        <div class="modal-scroll lore-modal-form">
          <div class="lore-form-grid lore-cols-3">
            <label
              >条目 ID <FieldHelp tooltip="世界书条目的稳定标识；脚本或外部工具可能依赖它。" /><input
                v-model="loreDialog.entry.id" /></label
            ><label
              >条目名称<input
                v-model="loreDialog.entry.comment"
                :placeholder="loreDialog.entry.name ? `留空时使用 name：${loreDialog.entry.name}` : ''"
                autofocus
            /></label>
            <label
              >条目用途<select
                :value="entryAudience(loreDialog.entry)"
                @change="setAudience(loreDialog.entry, ($event.target as HTMLSelectElement).value)"
              >
                <option v-for="(label, key) in audiences" :key="key" :value="key">{{ label }}</option>
              </select></label
            >
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
            ><label class="check"><input type="checkbox" v-model="loreDialog.entry.constant" />常驻条目</label
            ><label class="check"
              ><input
                type="checkbox"
                :checked="loreFlagValue(loreDialog.entry, 'caseSensitive')"
                @change="
                  setLoreFlag(loreDialog.entry, 'caseSensitive', ($event.target as HTMLInputElement).checked)
                " />区分大小写<FieldHelp :tooltip="loreFlagHelp(loreDialog.entry, 'caseSensitive')" /></label
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
                " />全词匹配<FieldHelp :tooltip="loreFlagHelp(loreDialog.entry, 'matchWholeWords')" /></label
            ><label class="check"
              ><input type="checkbox" v-model="loreDialog.entry.use_regex" />正则触发</label
            >
          </div>
          <div class="lore-form-grid">
            <label
              >主触发词（逗号分隔）<input
                :value="listValue(loreDialog.entry.keys ?? loreDialog.entry.key)"
                @change="setList(loreDialog.entry, 'keys', 'key', $event)" /></label
            ><label
              >辅助触发词（逗号分隔）
              <FieldHelp
                tooltip="与主触发词一起参与选择逻辑；是否需要任一、全部或排除，由“辅助词逻辑”决定。" /><input
                :value="listValue(loreDialog.entry.secondary_keys ?? loreDialog.entry.keysecondary)"
                @change="setList(loreDialog.entry, 'secondary_keys', 'keysecondary', $event)"
            /></label>
          </div>
          <label class="lore-content-field"
            >内容<ExpandableTextarea v-model="loreDialog.entry.content" label="世界书内容"
          /></label>
          <div class="lore-form-grid lore-cols-4">
            <label
              >插入顺序 <FieldHelp :tooltip="fieldTip(loreDialog.entry, 'insertion_order')" /><input
                type="number"
                :value="loreField(loreDialog.entry, 'insertion_order')"
                @input="setLoreField(loreDialog.entry, 'insertion_order', numberValue($event))" /></label
            ><label
              >插入位置 <FieldHelp tooltip="滚动方案下仅常驻使用；ST 方案按此位置注入。" /><input
                type="number"
                :value="positionValue(loreDialog.entry)"
                @input="setPosition(loreDialog.entry, $event)" /></label
            ><label
              >插入深度 <FieldHelp :tooltip="fieldTip(loreDialog.entry, 'depth')" /><input
                type="number"
                min="0"
                :value="loreField(loreDialog.entry, 'depth')"
                @input="setLoreField(loreDialog.entry, 'depth', numberValue($event))" /></label
            ><label
              >扫描深度 <FieldHelp :tooltip="fieldTip(loreDialog.entry, 'scan_depth')" /><input
                type="number"
                min="0"
                :value="loreField(loreDialog.entry, 'scan_depth')"
                @input="setLoreField(loreDialog.entry, 'scan_depth', numberValue($event))" /></label
            ><label
              >触发概率 % <FieldHelp :tooltip="fieldTip(loreDialog.entry, 'probability')" /><input
                type="number"
                min="0"
                max="100"
                :value="loreField(loreDialog.entry, 'probability')"
                @input="setLoreField(loreDialog.entry, 'probability', numberValue($event))" /></label
            ><label
              >辅助词逻辑 <FieldHelp :tooltip="fieldTip(loreDialog.entry, 'selectiveLogic')" /><select
                :value="loreField(loreDialog.entry, 'selectiveLogic') ?? 0"
                @change="
                  setLoreField(
                    loreDialog.entry,
                    'selectiveLogic',
                    Number(($event.target as HTMLSelectElement).value),
                  )
                "
              >
                <option :value="0">任一命中</option>
                <option :value="1">并非全部</option>
                <option :value="2">全部不命中</option>
                <option :value="3">全部命中</option>
              </select></label
            ><label
              >恢复尺度（轮） <FieldHelp :tooltip="fieldTip(loreDialog.entry, 'cooldown')" /><input
                type="number"
                min="0"
                :value="loreField(loreDialog.entry, 'cooldown')"
                @input="setLoreField(loreDialog.entry, 'cooldown', numberValue($event))" /></label
            ><label
              >延迟轮数 <FieldHelp :tooltip="fieldTip(loreDialog.entry, 'delay')" /><input
                type="number"
                min="0"
                :value="loreField(loreDialog.entry, 'delay')"
                @input="setLoreField(loreDialog.entry, 'delay', numberValue($event))" /></label
            ><label
              >消息角色 <FieldHelp :tooltip="fieldTip(loreDialog.entry, 'role')" /><input
                :value="loreField(loreDialog.entry, 'role')"
                @input="
                  setLoreField(loreDialog.entry, 'role', ($event.target as HTMLInputElement).value)
                " /></label
            ><label
              >分组 <FieldHelp :tooltip="fieldTip(loreDialog.entry, 'group')" /><input
                :value="loreField(loreDialog.entry, 'group')"
                @input="
                  setLoreField(loreDialog.entry, 'group', ($event.target as HTMLInputElement).value)
                " /></label
            ><label
              >分组权重 <FieldHelp :tooltip="fieldTip(loreDialog.entry, 'group_weight')" /><input
                type="number"
                :value="loreField(loreDialog.entry, 'group_weight')"
                @input="setLoreField(loreDialog.entry, 'group_weight', numberValue($event))"
            /></label>
          </div>
          <div class="lore-checks">
            <label class="check"
              ><input
                type="checkbox"
                :checked="Boolean(loreField(loreDialog.entry, 'useProbability') ?? true)"
                @change="
                  setLoreField(
                    loreDialog.entry,
                    'useProbability',
                    ($event.target as HTMLInputElement).checked,
                  )
                " />使用概率<FieldHelp :tooltip="fieldTip(loreDialog.entry, 'useProbability')" /></label
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
                " />阻止递归<FieldHelp :tooltip="loreFlagHelp(loreDialog.entry, 'preventRecursion')" /></label
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
  </Teleport>
</template>
