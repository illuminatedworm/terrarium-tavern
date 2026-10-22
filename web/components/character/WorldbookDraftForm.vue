<script setup lang="ts">
import { ref, watch } from 'vue';
import { emptyLoreFilter } from '../../lib/loreFilter';
import LoreBookEditor from './LoreBookEditor.vue';

/** Form for a Card Author worldbook draft: the document is only `{ name, character_book }`. */
const props = defineProps<{ document: { name?: string; character_book?: any }; draftKey: string }>();
const emit = defineEmits<{ draftSaved: [document: { name: string; character_book: any }]; back: [] }>();
const name = ref(props.document.name || ''),
  book = ref<any>(JSON.parse(JSON.stringify(props.document.character_book || { entries: [] })));
const loreFilter = ref(emptyLoreFilter());
const notice = ref(''),
  error = ref('');
// Like the card form, unsaved edits survive closing the dialog within this tab;
// saving the draft clears the key.
try {
  const saved = sessionStorage.getItem(props.draftKey);
  if (saved) {
    const restored = JSON.parse(saved);
    name.value = restored.name;
    book.value = restored.character_book;
    notice.value = '已恢复这个标签页中的未保存草稿。';
  }
} catch {
  /* A malformed local draft does not block the saved draft. */
}
watch(
  [name, book],
  () => {
    if (
      name.value !== (props.document.name || '') ||
      JSON.stringify(book.value) !== JSON.stringify(props.document.character_book || { entries: [] })
    )
      sessionStorage.setItem(
        props.draftKey,
        JSON.stringify({ name: name.value, character_book: book.value }),
      );
    else sessionStorage.removeItem(props.draftKey);
  },
  { deep: true },
);
</script>

<template>
  <section class="worldbook-draft-form" aria-label="世界书草稿表单">
    <h2>编辑世界书草稿</h2>
    <p class="muted">修改只进入制卡草稿；在“修改”页审阅后保存回世界书库。</p>
    <AppNotification :message="error" />
    <AppNotification :message="notice" />
    <label>世界书名称<input v-model="name" maxlength="200" /></label>
    <LoreBookEditor
      v-model:book="book"
      :book-name="name"
      :filter="loreFilter"
      library
      save-hint="保存草稿后进入制卡草稿"
      @notice="notice = $event"
      @error="error = $event"
    />
    <div class="toolbar">
      <button @click="emit('back')">返回</button>
      <button
        class="primary"
        :disabled="!name.trim()"
        @click="emit('draftSaved', { name: name.trim(), character_book: book })"
      >
        保存草稿
      </button>
    </div>
  </section>
</template>
