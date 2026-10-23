<script setup lang="ts">
import Icon from '../common/Icon.vue';
import LoreBookEditor from './LoreBookEditor.vue';
import { useTimedError } from '../../composables/useTimedError';
import { computed, ref, watch } from 'vue';
import { api } from '../../lib/api';
import { downloadLink } from '../../lib/download';
import { emptyLoreFilter } from '../../lib/loreFilter';
import type { Worldbook } from '../../lib/types';

/** Full-page editor for one library worldbook; unsaved edits survive in this tab. */
const clone = (value: any) => JSON.parse(JSON.stringify(value));
const props = defineProps<{ worldbook: Worldbook }>();
const emit = defineEmits<{ saved: [book: Worldbook]; back: [] }>();
const saved = ref<Worldbook>(clone(props.worldbook));
const name = ref(saved.value.name),
  book = ref<any>(clone(saved.value.book));
// The revision the edits started from; a draft restored from an older revision
// must fail with a conflict instead of overwriting newer library content.
let baseRevision = saved.value.revision;
const loreFilter = ref(emptyLoreFilter());
const loreEditor = ref<InstanceType<typeof LoreBookEditor>>();
const busy = ref(false),
  confirmingRestore = ref(false),
  error = ref(''),
  notice = ref('');
const draftKey = `tavern-worldbook-draft-${props.worldbook.id}`;
const entryCount = computed(() => Object.keys(book.value.entries || {}).length);
const hasUnsavedChanges = computed(
  () => name.value !== saved.value.name || JSON.stringify(book.value) !== JSON.stringify(saved.value.book),
);
try {
  const draft = sessionStorage.getItem(draftKey);
  if (draft) {
    ({ name: name.value, book: book.value, revision: baseRevision } = JSON.parse(draft));
    notice.value = '已恢复这个标签页中的未保存草稿。';
  }
} catch {
  /* A malformed local draft does not block the saved worldbook. */
}
watch(
  [name, book],
  () => {
    if (hasUnsavedChanges.value)
      sessionStorage.setItem(
        draftKey,
        JSON.stringify({ name: name.value, book: book.value, revision: baseRevision }),
      );
    else sessionStorage.removeItem(draftKey);
  },
  { deep: true },
);
async function save() {
  if (busy.value) return;
  busy.value = true;
  error.value = '';
  try {
    // A conflict keeps the draft in this tab, so the user can reload and reapply it.
    const result: Worldbook = await api(
      `/worldbooks/${saved.value.id}`,
      { name: name.value, book: book.value, expected_revision: baseRevision },
      'PUT',
    );
    saved.value = result;
    baseRevision = result.revision;
    name.value = result.name;
    book.value = clone(result.book);
    sessionStorage.removeItem(draftKey);
    notice.value = '世界书已保存。已挂载它的对话不会自动更新，可在对话中重新载入。';
    emit('saved', result);
  } catch (e: any) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
async function restore() {
  if (busy.value) return;
  busy.value = true;
  error.value = '';
  try {
    // Fetch before clearing the draft so a failed reload preserves all edits.
    const latest: Worldbook = await api(`/worldbooks/${saved.value.id}`);
    saved.value = latest;
    baseRevision = latest.revision;
    name.value = latest.name;
    book.value = clone(latest.book);
    sessionStorage.removeItem(draftKey);
    loreEditor.value?.close();
    confirmingRestore.value = false;
    notice.value = '已放弃未保存修改，恢复已保存的世界书。';
  } catch (e: any) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
useTimedError(error);
</script>

<template>
  <section class="editor-page">
    <div class="editor-header">
      <div class="character-header-main">
        <button class="quiet editor-back" @click="emit('back')">
          <Icon name="arrow-left" :size="16" />返回
        </button>
        <div class="character-heading">
          <div>
            <h1>{{ name || '世界书' }}</h1>
            <p class="muted">
              世界书库 · {{ entryCount }} 个条目<span v-if="hasUnsavedChanges"> · 有未保存修改</span>
            </p>
          </div>
        </div>
      </div>
      <div class="toolbar character-editor-actions">
        <a
          class="editor-export"
          :href="`/api/worldbooks/${saved.id}/export`"
          title="导出已保存的内容"
          @click="downloadLink"
          ><Icon name="import" :size="16" />导出世界书</a
        >
        <template v-if="confirmingRestore">
          <button class="danger" :disabled="busy" @click="restore">确认放弃修改</button>
          <button :disabled="busy" @click="confirmingRestore = false">继续编辑</button>
        </template>
        <button v-else :disabled="busy || !hasUnsavedChanges" @click="confirmingRestore = true">
          <Icon name="refresh" :size="15" />恢复原稿状态
        </button>
        <button class="primary" :disabled="busy || !name.trim()" @click="save">
          {{ busy ? '正在保存…' : '保存世界书' }}
        </button>
      </div>
    </div>
    <AppNotification :message="error" />
    <AppNotification :message="notice" />
    <div class="editor-layout character-editor-layout">
      <div class="editor-fields">
        <label>世界书名称<input v-model="name" maxlength="200" /></label>
        <LoreBookEditor
          ref="loreEditor"
          v-model:book="book"
          :book-name="name"
          :filter="loreFilter"
          library
          save-hint="保存世界书后生效"
          @notice="notice = $event"
          @error="error = $event"
        />
      </div>
    </div>
  </section>
</template>
