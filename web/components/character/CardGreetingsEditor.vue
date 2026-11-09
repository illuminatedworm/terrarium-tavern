<script setup lang="ts">
import { computed, nextTick, ref, useId, watch } from 'vue';
import { openingProse } from '../../lib/storyText';
import ExpandableTextarea from '../common/ExpandableTextarea.vue';
import Icon from '../common/Icon.vue';
import CardPreviewProse from './CardPreviewProse.vue';

const props = defineProps<{ greetings: string[]; disabled: boolean }>();
const emit = defineEmits<{ 'update:greetings': [greetings: string[]]; notice: [message: string] }>();
type DeletedGreeting = {
  id: number;
  index: number;
  text: string;
  defaultMoves: number;
  placeholder?: number;
};

// Keep row identity through reordering, even when two openings have identical text.
let nextId = 0;
const ids = ref(props.greetings.map(() => nextId++));
const selected = ref(ids.value[0]);
const deleted = ref<DeletedGreeting[]>([]);
let defaultMoves = 0;
const selectedIndex = computed(() => ids.value.indexOf(selected.value));
const current = computed(() => props.greetings[selectedIndex.value] ?? '');
const snippets = computed(() =>
  props.greetings.map((text) => {
    const prefix = text.trimStart().slice(0, 160);
    return (
      openingProse(prefix)
        .replace(/<(?:initvar|UpdateVariable)\b[\s\S]*$/i, '')
        .replace(/<[^>]*>|\s+/g, ' ')
        .trim()
        .slice(0, 72) || (prefix ? '查看开头预览' : '暂无正文')
    );
  }),
);
const listOpen = ref(false);
const listId = useId();
const listToggle = ref<HTMLButtonElement>();
const editor = ref<InstanceType<typeof ExpandableTextarea>>();
const preview = ref<HTMLElement>();
const previewBody = ref<HTMLElement>();
const hasMore = ref(false);
const remove = ref<HTMLButtonElement>();
const dragged = ref<number>();
const dropMarker = ref<{ id: number; after: boolean }>();

watch([preview, previewBody], ([viewport, body], _, onCleanup) => {
  hasMore.value = false;
  if (!viewport || !body) return;
  const observer = new ResizeObserver(() => {
    hasMore.value = body.offsetHeight > viewport.clientHeight;
  });
  observer.observe(viewport);
  observer.observe(body);
  onCleanup(() => observer.disconnect());
});

async function select(id: number, fromList = false) {
  selected.value = id;
  const wasOpen = listOpen.value;
  listOpen.value = false;
  if (fromList && wasOpen) {
    await nextTick();
    if (listToggle.value?.offsetWidth) listToggle.value.focus({ preventScroll: true });
  }
}
function editText(text: string) {
  const texts = props.greetings.slice();
  texts[selectedIndex.value] = text;
  emit('update:greetings', texts);
}
async function add() {
  const id = nextId++;
  ids.value.push(id);
  emit('update:greetings', [...props.greetings, '']);
  select(id);
  await nextTick();
  editor.value?.openEditor();
}
function move(from: number, to: number) {
  if (from === to) return;
  const texts = props.greetings.slice();
  texts.splice(to, 0, texts.splice(from, 1)[0]);
  ids.value.splice(to, 0, ids.value.splice(from, 1)[0]);
  if (from === 0 || to === 0) defaultMoves++;
  emit('update:greetings', texts);
  emit('notice', `已移到开场 ${to + 1}，保存角色后生效。`);
}
function removeSelected() {
  const index = selectedIndex.value;
  const id = selected.value;
  const texts = props.greetings.slice();
  const [text] = texts.splice(index, 1);
  ids.value.splice(index, 1);
  const removed: DeletedGreeting = { id, index, text, defaultMoves };
  if (texts.length === 0) {
    removed.placeholder = nextId++;
    ids.value.push(removed.placeholder);
    texts.push('');
  }
  deleted.value.push(removed);
  selected.value = ids.value[Math.min(index, ids.value.length - 1)];
  emit('update:greetings', texts);
  emit('notice', removed.placeholder === undefined ? '已删除开场白，保存角色后生效。' : '已清空开场白。');
}
async function undoDelete() {
  const removed = deleted.value.pop();
  if (!removed) return;
  const texts = props.greetings.slice();
  const placeholderIndex = ids.value.findIndex((id) => id === removed.placeholder);
  const editedPlaceholder = placeholderIndex >= 0 && texts[placeholderIndex] !== '';
  if (placeholderIndex >= 0 && !editedPlaceholder) {
    texts.splice(placeholderIndex, 1);
    ids.value.splice(placeholderIndex, 1);
  }
  // Restore the old position where possible without replacing a later explicit
  // default choice or any text written into the slot created by clearing.
  const preserveDefault = texts.length > 0 && (removed.defaultMoves !== defaultMoves || editedPlaceholder);
  const index = Math.max(preserveDefault ? 1 : 0, Math.min(removed.index, texts.length));
  texts.splice(index, 0, removed.text);
  ids.value.splice(index, 0, removed.id);
  emit('update:greetings', texts);
  select(removed.id);
  emit('notice', '已撤销删除开场白。');
  if (!deleted.value.length) {
    await nextTick();
    remove.value?.focus({ preventScroll: true });
  }
}

function startDrag(event: DragEvent, id: number) {
  if (props.disabled || !event.dataTransfer) return;
  dragged.value = id;
  event.dataTransfer.effectAllowed = 'move';
  event.dataTransfer.setData('application/x-tavern-greeting', String(id));
}
function dragOver(event: DragEvent, id: number) {
  if (dragged.value === undefined || props.disabled) return;
  event.preventDefault();
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
  dropMarker.value = { id, after: dropsAfterRow(event) };
}
function dropsAfterRow(event: DragEvent) {
  const row = (event.currentTarget as HTMLElement).getBoundingClientRect();
  return event.clientY >= row.top + row.height / 2;
}
function leaveRow(event: DragEvent, id: number) {
  if (
    dropMarker.value?.id === id &&
    !(
      event.relatedTarget instanceof Node &&
      (event.currentTarget as HTMLElement).contains(event.relatedTarget)
    )
  )
    dropMarker.value = undefined;
}
function finishDrag() {
  dragged.value = undefined;
  dropMarker.value = undefined;
}
function drop(event: DragEvent, index: number) {
  if (dragged.value === undefined || props.disabled) return;
  event.preventDefault();
  const from = ids.value.indexOf(dragged.value);
  const insertion = index + Number(dropsAfterRow(event));
  move(from, insertion > from ? insertion - 1 : insertion);
  finishDrag();
}
</script>

<template>
  <section class="card-greetings" aria-label="开场白">
    <header class="greetings-heading">
      <h2>
        开场白 <span class="greetings-count">{{ greetings.length }} 条</span>
      </h2>
      <p>开场 1 为默认，顺序与开局一致。保存角色后生效。</p>
      <button type="button" :disabled="disabled" @click="add"><Icon name="plus" :size="16" />新增开场</button>
    </header>
    <div class="greetings-layout">
      <div class="greetings-rail">
        <button
          ref="listToggle"
          type="button"
          class="greetings-list-toggle"
          :aria-expanded="listOpen"
          :aria-controls="listId"
          @click="listOpen = !listOpen"
        >
          <span class="greeting-row-text">
            <span class="greeting-row-title">
              <strong>开场 {{ selectedIndex + 1 }}</strong>
              <span v-if="selectedIndex === 0" class="greeting-default">默认</span>
            </span>
            <span class="greeting-snippet">{{ snippets[selectedIndex] }}</span>
          </span>
          <Icon :name="listOpen ? 'chevron-up' : 'chevron-down'" :size="16" />
        </button>
        <ol :id="listId" class="greetings-list" :class="{ 'is-open': listOpen }" aria-label="开场列表">
          <li
            v-for="(id, index) in ids"
            :key="id"
            class="greeting-row"
            :class="{
              'is-selected': selected === id,
              'is-dragged': dragged === id,
              'drop-before': dropMarker?.id === id && !dropMarker.after && dragged !== id,
              'drop-after': dropMarker?.id === id && dropMarker.after && dragged !== id,
            }"
            @dragover="dragOver($event, id)"
            @dragleave="leaveRow($event, id)"
            @drop="drop($event, index)"
          >
            <button
              type="button"
              class="greeting-row-text"
              :aria-pressed="selected === id"
              @click="select(id, true)"
            >
              <span class="greeting-row-title">
                <strong>开场 {{ index + 1 }}</strong>
                <span v-if="index === 0" class="greeting-default">默认</span>
              </span>
              <span class="greeting-snippet">{{ snippets[index] }}</span>
            </button>
            <span
              v-if="ids.length > 1"
              class="greeting-grip"
              :draggable="!disabled"
              title="拖动排序"
              aria-hidden="true"
              @dragstart="startDrag($event, id)"
              @dragend="finishDrag"
            >
              <Icon name="grip" :size="17" />
            </span>
          </li>
        </ol>
        <button v-if="deleted.length" type="button" class="quiet" :disabled="disabled" @click="undoDelete">
          <Icon name="refresh" :size="15" />撤销删除
        </button>
      </div>
      <div class="greeting-preview">
        <div class="greeting-preview-heading">
          <h3>
            开场 {{ selectedIndex + 1 }} <span v-if="selectedIndex === 0" class="greeting-default">默认</span>
          </h3>
          <div class="greeting-actions">
            <ExpandableTextarea
              ref="editor"
              :model-value="current"
              :label="`开场 ${selectedIndex + 1}`"
              :disabled="disabled"
              dialog-only
              @update:model-value="editText"
            />
            <button
              ref="remove"
              type="button"
              class="greeting-delete"
              :disabled="disabled || (ids.length === 1 && !current)"
              @click="removeSelected"
            >
              <Icon name="trash" :size="16" />{{ ids.length === 1 ? '清空' : '删除' }}
            </button>
          </div>
        </div>
        <div ref="preview" class="greeting-preview-viewport" :class="{ 'has-more': hasMore }">
          <div ref="previewBody">
            <CardPreviewProse v-if="current.trim()" :text="current" opening excerpt />
            <p v-else class="greeting-empty">这条开场还没有正文。</p>
          </div>
        </div>
        <p class="greeting-preview-caption">
          <Icon name="eye" :size="15" />
          {{ hasMore ? '开头预览 · 还有正文' : '正文预览' }}
        </p>
      </div>
    </div>
  </section>
</template>

<style scoped>
.card-greetings {
  display: grid;
  gap: 14px;
  padding: 18px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  background: var(--surface-panel);
}
.greetings-heading {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 4px 8px;
  align-items: center;
}
.greetings-heading button {
  grid-area: 1 / 2 / span 2;
}
.greeting-preview-heading,
.greeting-actions,
.greeting-preview-caption {
  display: flex;
  align-items: center;
  gap: 8px;
}
.greeting-preview-heading {
  justify-content: space-between;
  flex-wrap: wrap;
}
.greetings-heading h2,
.greeting-preview-heading h3 {
  margin: 0;
  font-size: var(--fs-lg);
  letter-spacing: normal;
}
.greetings-count,
.greetings-heading p,
.greeting-snippet,
.greeting-preview-caption,
.greeting-empty {
  color: var(--text-muted);
  font-size: var(--fs-sm);
}
.greetings-count {
  margin-inline-start: 6px;
  font-weight: 400;
}
.greetings-heading p {
  margin: 0;
}
.greetings-layout {
  display: grid;
  grid-template-columns: minmax(155px, 25%) minmax(0, 1fr);
  gap: 16px;
}
.greetings-rail {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.greetings-list {
  display: grid;
  align-content: start;
  gap: 4px;
  /* The preview sets the grid row height; the list fills the available space. */
  flex: 1;
  block-size: 0;
  overflow: auto;
  padding: 2px;
  margin: 0;
  list-style: none;
}
.greetings-rail > .quiet {
  align-self: flex-start;
  margin-top: 8px;
}
.greeting-row {
  position: relative;
  display: flex;
  align-items: center;
  gap: 2px;
  min-width: 0;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
}
.greeting-row.is-selected {
  border-color: var(--accent-glow);
  background: var(--accent-soft);
}
.greeting-row.is-dragged {
  opacity: 0.5;
}
.greeting-row.drop-before {
  border-top-color: var(--accent);
}
.greeting-row.drop-after {
  border-bottom-color: var(--accent);
}
.greeting-row-text {
  display: grid;
  gap: 3px;
  flex: 1;
  min-width: 0;
  padding: 8px 10px;
  border: 0;
  background: transparent;
  text-align: start;
}
.greeting-row-text:hover:not(:disabled) {
  background: var(--hover-wash);
}
.greeting-row-title {
  display: flex;
  align-items: center;
  gap: 6px;
}
.greeting-snippet {
  display: block;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
/* Neutral chip: the accent is reserved for the selected row. */
.greeting-default {
  padding: 1px 6px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-xs);
  background: var(--hover-wash);
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
  font-weight: 500;
}
.greeting-grip {
  display: grid;
  place-items: center;
  padding: 8px 4px;
  color: var(--text-secondary);
  cursor: grab;
}
.greeting-grip:hover {
  color: var(--accent-hover);
}
.greeting-grip > .icon {
  pointer-events: none;
}
/* Pointer devices reveal the grip on the row being handled; touch keeps it shown. */
@media (hover: hover) {
  .greeting-grip {
    opacity: 0;
    transition: opacity var(--dur) var(--ease-out);
  }
  .greeting-row:hover .greeting-grip,
  .greeting-row:focus-within .greeting-grip,
  .greeting-row.is-selected .greeting-grip {
    opacity: 1;
  }
}
.greetings-list-toggle {
  display: none;
}
.greeting-preview {
  min-width: 0;
  padding: 12px 16px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--surface-input);
}
.greeting-preview-heading {
  padding-bottom: 10px;
  border-bottom: 1px solid var(--border-subtle);
}
.greeting-preview-heading h3 {
  display: flex;
  align-items: center;
  gap: 8px;
}
/* Deleting is undoable, so it stays a calm outline button and turns red on hover. */
.greeting-delete {
  color: var(--text-secondary);
}
.greeting-delete:hover:not(:disabled) {
  border-color: var(--danger-edge);
  background: var(--danger-surface);
  color: var(--danger);
}
.greeting-preview-viewport {
  position: relative;
  max-block-size: 8lh;
  overflow: clip;
  margin: 16px 0 10px;
  font: var(--story-font-size, var(--fs-base)) / var(--story-line-height, 1.85)
    var(--story-font-family, var(--font-ui));
}
.greeting-preview-viewport.has-more::after {
  position: absolute;
  inset: auto 0 0;
  height: 1lh;
  background: linear-gradient(transparent, var(--surface-input));
  pointer-events: none;
  content: '';
}
.greeting-preview-viewport :deep(.card-preview-prose) {
  font: inherit;
}
.greeting-empty,
.greeting-preview-caption {
  margin: 0;
}
.greeting-preview-caption {
  min-height: 20px;
  gap: 6px;
}
@media (max-width: 620px) {
  .card-greetings {
    padding: 12px;
  }
  .greetings-heading button {
    grid-row: 1;
  }
  .greetings-heading p {
    grid-column: 1 / -1;
  }
  .greetings-layout {
    grid-template-columns: minmax(0, 1fr);
    gap: 10px;
  }
  .greetings-list-toggle {
    display: flex;
    align-items: center;
    width: 100%;
    gap: 8px;
  }
  .greetings-list-toggle .greeting-row-text {
    padding: 0;
  }
  .greetings-list:not(.is-open),
  .greeting-grip {
    display: none;
  }
  .greetings-list.is-open {
    flex: none;
    block-size: auto;
    max-block-size: 20rem;
    margin-top: 4px;
  }
  .greeting-preview {
    padding-inline: 12px;
  }
}
</style>
