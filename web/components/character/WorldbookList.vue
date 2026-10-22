<script setup lang="ts">
import { ref } from 'vue';
import Icon from '../common/Icon.vue';
import type { WorldbookSummary } from '../../lib/types';

/** Left sidebar list of library worldbooks. */
defineProps<{ worldbooks: WorldbookSummary[]; activeId?: string }>();
const emit = defineEmits<{
  create: [];
  import: [file: File];
  show: [book: WorldbookSummary];
  delete: [book: WorldbookSummary, event: MouseEvent];
}>();
const fileInput = ref<HTMLInputElement>();
function chosen(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  // Clear first so choosing the same file again still fires change.
  input.value = '';
  if (file) emit('import', file);
}
</script>

<template>
  <header class="panel-head">
    <h2>世界书</h2>
    <span class="panel-count">{{ worldbooks.length }}</span>
  </header>
  <div class="panel-actions">
    <button class="new-story" aria-label="新建世界书" title="新建世界书" @click="emit('create')">
      <Icon name="plus" :size="17" />新建
    </button>
    <button
      class="new-story"
      aria-label="导入世界书 JSON"
      title="导入 SillyTavern 世界书或 V2 角色卡世界书 JSON"
      @click="fileInput?.click()"
    >
      <Icon name="import" :size="17" />导入
    </button>
  </div>
  <input ref="fileInput" type="file" accept=".json" hidden @change="chosen" />
  <nav class="session-list" aria-label="世界书列表">
    <div
      v-for="book in worldbooks"
      :key="book.id"
      class="session-row"
      :data-worldbook-id="book.id"
      :class="{ active: book.id === activeId }"
    >
      <button class="session-open" @click="emit('show', book)">
        <span class="session-glyph"><Icon name="book" :size="17" /></span>
        <span
          >{{ book.name }}<small>{{ book.entries }} 个条目</small></span
        >
      </button>
      <span class="row-actions"
        ><button
          class="session-delete"
          :aria-label="`删除世界书 ${book.name}`"
          title="删除世界书"
          @click.stop="emit('delete', book, $event)"
        >
          <Icon name="trash" :size="15" /></button
      ></span>
    </div>
    <p v-if="!worldbooks.length" class="empty-small">
      新建世界书，或导入 SillyTavern 世界书与 V2 角色卡世界书 JSON。
    </p>
  </nav>
</template>
