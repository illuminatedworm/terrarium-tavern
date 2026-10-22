<script setup lang="ts">
import AppModal from '../common/AppModal.vue';
import Icon from '../common/Icon.vue';
import { computed, onMounted, ref } from 'vue';
import { api } from '../../lib/api';
import type { WorldbookMount, WorldbookSummary } from '../../lib/types';

/**
 * Adjusts which library worldbooks are copied into this conversation. Each
 * change commits as one content edit of the latest reply, like a manual edit.
 */
const props = defineProps<{ session: any; document: any; blocked: string }>();
const emit = defineEmits<{ close: []; mounted: [session: any] }>();
const registry = computed<WorldbookMount[]>(() => props.session.snapshot.worldbooks || []);
const library = ref<WorldbookSummary[]>([]);
const keep = ref(new Set(registry.value.map((item) => item.id)));
const reload = ref(new Set<string>());
const add = ref(new Set<string>());
const loading = ref(true),
  busy = ref(false),
  error = ref('');
const libraryById = computed(() => new Map(library.value.map((book) => [book.id, book])));
const available = computed(() =>
  library.value.filter((book) => !registry.value.some((item) => item.id === book.id)),
);
// Mounted copies always carry an explicit ID (and `extensions.id` when the source had one).
const present = computed(() => {
  const entries = (props.document.data || props.document).character_book?.entries || [];
  return new Set(
    (Array.isArray(entries) ? entries : Object.values(entries)).map((entry: any) =>
      String(entry.extensions?.id ?? entry.id),
    ),
  );
});
const removing = computed(() =>
  registry.value.filter((item) => !keep.value.has(item.id) || reload.value.has(item.id)),
);
const payload = computed(() => ({
  worldbook_ids: [
    ...registry.value.filter((item) => keep.value.has(item.id)).map((item) => item.id),
    ...available.value.filter((book) => add.value.has(book.id)).map((book) => book.id),
  ],
  reload: registry.value
    .filter((item) => keep.value.has(item.id) && reload.value.has(item.id))
    .map((item) => item.id),
}));
const changed = computed(
  () =>
    !!payload.value.reload.length ||
    JSON.stringify(payload.value.worldbook_ids) !== JSON.stringify(registry.value.map((item) => item.id)),
);

function status(item: WorldbookMount) {
  const book = libraryById.value.get(item.id);
  if (!book) return '书库中已删除';
  if (book.revision !== item.revision) return '书库已更新';
  return '与挂载时一致';
}
function copies(item: WorldbookMount) {
  return item.entries.filter((id) => present.value.has(id)).length;
}
const choices = { keep, reload, add };
function toggle(name: keyof typeof choices, id: string, on: boolean) {
  const next = new Set(choices[name].value);
  if (on) next.add(id);
  else next.delete(id);
  choices[name].value = next;
}
async function load() {
  loading.value = true;
  error.value = '';
  try {
    library.value = await api('/worldbooks');
  } catch (e: any) {
    error.value = e.message;
  } finally {
    loading.value = false;
  }
}
async function submit() {
  if (busy.value || !changed.value) return;
  busy.value = true;
  error.value = '';
  try {
    const updated = await api(
      `/sessions/${props.session.id}/worldbooks`,
      { ...payload.value, expected_revision: props.session.revision, operation_id: crypto.randomUUID() },
      'PUT',
    );
    emit('mounted', updated);
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
      <AppModal label="挂载世界书" size="medium" @close="!busy && emit('close')">
        <span class="eyebrow">对话级角色卡</span>
        <h2>挂载世界书</h2>
        <p class="muted">
          挂载会把书库世界书的条目复制进本对话，之后修改书库不会自动同步，需在这里重新载入。调整作为一次设定修改提交，生成最新回复的新版本；采用“审核后保存”时，接受后生效。
        </p>
        <div v-if="loading" class="empty-small" role="status">正在读取世界书库…</div>
        <div v-else-if="error && !library.length && !registry.length" class="card-sync-empty">
          <p role="alert">{{ error }}</p>
          <button @click="load"><Icon name="refresh" :size="15" />重试</button>
        </div>
        <template v-else>
          <h3>已挂载</h3>
          <p v-if="!registry.length" class="empty-small">本对话尚未挂载世界书。</p>
          <div v-else class="worldbook-choice-list">
            <div v-for="item in registry" :key="item.id" class="worldbook-mounted">
              <label class="check worldbook-choice">
                <input
                  type="checkbox"
                  :checked="keep.has(item.id)"
                  :disabled="busy"
                  :aria-label="`保持挂载 ${item.name}`"
                  @change="toggle('keep', item.id, ($event.target as HTMLInputElement).checked)"
                /><span>{{ item.name }}</span
                ><small>本对话 {{ copies(item) }} 条 · {{ status(item) }}</small>
              </label>
              <label v-if="libraryById.has(item.id) && keep.has(item.id)" class="check">
                <input
                  type="checkbox"
                  :checked="reload.has(item.id)"
                  :disabled="busy"
                  @change="toggle('reload', item.id, ($event.target as HTMLInputElement).checked)"
                />重新载入书库最新内容
              </label>
            </div>
          </div>
          <h3>书库中的其他世界书</h3>
          <p v-if="!available.length" class="empty-small">没有可挂载的其他世界书。</p>
          <div v-else class="worldbook-choice-list">
            <label v-for="book in available" :key="book.id" class="check worldbook-choice">
              <input
                type="checkbox"
                :checked="add.has(book.id)"
                :disabled="busy"
                :aria-label="`挂载 ${book.name}`"
                @change="toggle('add', book.id, ($event.target as HTMLInputElement).checked)"
              /><span>{{ book.name }}</span
              ><small>{{ book.entries }} 条</small>
            </label>
          </div>
          <p v-if="removing.length" class="card-sync-message" role="status">
            卸载或重新载入会移除「{{
              removing.map((item) => item.name).join('」「')
            }}」在本对话中的全部条目，包括对话中对它们的修改。
          </p>
        </template>
        <footer class="modal-footer card-sync-footer">
          <p v-if="error && (library.length || registry.length)" class="card-sync-message" role="alert">
            {{ error }}
          </p>
          <p v-else-if="blocked" class="card-sync-message">{{ blocked }}</p>
          <button :disabled="busy" @click="emit('close')">取消</button>
          <button class="primary" :disabled="busy || loading || !!blocked || !changed" @click="submit">
            {{ busy ? '正在提交…' : '确认调整' }}
          </button>
        </footer>
      </AppModal>
    </div>
  </Teleport>
</template>
