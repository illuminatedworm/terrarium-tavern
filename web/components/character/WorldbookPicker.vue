<script setup lang="ts">
import AppModal from '../common/AppModal.vue';
import Icon from '../common/Icon.vue';
import { computed, onMounted, ref } from 'vue';
import { api } from '../../lib/api';
import type { Worldbook, WorldbookSummary } from '../../lib/types';

/** Chooses one library worldbook whose entries the caller appends to its draft. */
const emit = defineEmits<{ close: []; picked: [book: Worldbook] }>();
const books = ref<WorldbookSummary[]>([]);
const query = ref('');
const loading = ref(true),
  busy = ref(false),
  error = ref('');
const visible = computed(() => {
  const text = query.value.trim().toLowerCase();
  return books.value.filter((book) => book.name.toLowerCase().includes(text));
});

async function load() {
  loading.value = true;
  error.value = '';
  try {
    books.value = await api('/worldbooks');
  } catch (e: any) {
    error.value = e.message;
  } finally {
    loading.value = false;
  }
}
async function pick(book: WorldbookSummary) {
  if (busy.value) return;
  busy.value = true;
  error.value = '';
  try {
    emit('picked', await api(`/worldbooks/${book.id}`));
  } catch (e: any) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
onMounted(load);
</script>

<template>
  <Teleport to="body">
    <div class="modal-backdrop" @keydown.esc.prevent.stop="!busy && emit('close')">
      <AppModal
        label="从世界书库导入"
        size="medium"
        class="worldbook-picker-modal"
        @close="!busy && emit('close')"
      >
        <span class="eyebrow">世界书库</span>
        <h2>从世界书库导入</h2>
        <p class="muted">所选世界书的全部条目追加到当前草稿，规则与导入世界书 JSON 相同；保存后生效。</p>
        <input v-model="query" type="search" placeholder="搜索世界书…" aria-label="搜索世界书" />
        <div v-if="loading" class="empty-small" role="status">正在读取世界书库…</div>
        <div v-else-if="error && !books.length" class="card-sync-empty">
          <p role="alert">{{ error }}</p>
          <button @click="load"><Icon name="refresh" :size="15" />重试</button>
        </div>
        <p v-else-if="!books.length" class="empty-small">世界书库为空，可在左侧“世界书”页新建或导入。</p>
        <p v-else-if="!visible.length" class="empty-small">没有名称匹配的世界书。</p>
        <div v-else class="worldbook-choice-list">
          <button
            v-for="book in visible"
            :key="book.id"
            class="worldbook-choice"
            :disabled="busy"
            :aria-label="`导入世界书 ${book.name}`"
            @click="pick(book)"
          >
            <Icon name="book" :size="16" /><span>{{ book.name }}</span
            ><small>{{ book.entries }} 条</small>
          </button>
        </div>
        <p v-if="error && books.length" class="card-sync-message" role="alert">{{ error }}</p>
      </AppModal>
    </div>
  </Teleport>
</template>
