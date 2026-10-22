<script setup lang="ts">
import AppModal from '../common/AppModal.vue';
import Icon from '../common/Icon.vue';
import { computed, onMounted, ref } from 'vue';
import { api } from '../../lib/api';
import type { CardDefaults, WorldbookSummary } from '../../lib/types';

/**
 * A shelf card's new-conversation defaults: one user persona and some library
 * worldbooks preselected when a story starts. They live beside the card, never
 * in the card file, and existing stories are unaffected.
 */
const props = defineProps<{ cardId: string }>();
const defaults = ref<CardDefaults | null>(null);
const personas = ref<{ id: string; name: string }[]>([]);
const worldbooks = ref<WorldbookSummary[]>([]);
const open = ref(false),
  loading = ref(true),
  busy = ref(false),
  error = ref('');
const personaId = ref(''),
  chosen = ref(new Set<string>());
const summary = computed(() => {
  const value = defaults.value;
  if (!value || (!value.persona_id && !value.worldbook_ids.length)) return '新建对话时不预选用户角色和世界书';
  const persona = personas.value.find((item) => item.id === value.persona_id)?.name;
  const books = value.worldbook_ids
    .map((id) => worldbooks.value.find((book) => book.id === id)?.name)
    .filter(Boolean);
  return [persona && `用户角色「${persona}」`, books.length && `世界书「${books.join('」「')}」`]
    .filter(Boolean)
    .join('，');
});
const count = computed(() =>
  defaults.value ? Number(!!defaults.value.persona_id) + defaults.value.worldbook_ids.length : 0,
);
const changed = computed(
  () =>
    !!defaults.value &&
    ((defaults.value.persona_id || '') !== personaId.value ||
      JSON.stringify(defaults.value.worldbook_ids) !== JSON.stringify(selectedIds())),
);

// Mount order follows the library list, the same order the new-story step shows.
function selectedIds() {
  return worldbooks.value.filter((book) => chosen.value.has(book.id)).map((book) => book.id);
}
async function load() {
  loading.value = true;
  error.value = '';
  try {
    [defaults.value, personas.value, worldbooks.value] = await Promise.all([
      api(`/cards/${props.cardId}/defaults`),
      api('/personas'),
      api('/worldbooks'),
    ]);
  } catch (e: any) {
    error.value = e.message;
  } finally {
    loading.value = false;
  }
}
async function show() {
  open.value = true;
  await load();
  personaId.value = defaults.value?.persona_id || '';
  chosen.value = new Set(defaults.value?.worldbook_ids || []);
}
function toggle(id: string, on: boolean) {
  const next = new Set(chosen.value);
  if (on) next.add(id);
  else next.delete(id);
  chosen.value = next;
}
async function save() {
  if (busy.value || !defaults.value) return;
  busy.value = true;
  error.value = '';
  try {
    defaults.value = await api(
      `/cards/${props.cardId}/defaults`,
      {
        persona_id: personaId.value || null,
        worldbook_ids: selectedIds(),
        expected_version: defaults.value.version,
      },
      'PUT',
    );
    open.value = false;
  } catch (e: any) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
onMounted(load);
</script>

<template>
  <button :title="summary" @click="show">
    <Icon name="sliders" :size="15" />新建对话默认项<span v-if="count" class="badge">{{ count }}</span>
  </button>
  <Teleport to="body">
    <div v-if="open" class="modal-backdrop" @keydown.esc.prevent.stop="!busy && (open = false)">
      <AppModal label="新建对话默认项" size="medium" @close="!busy && (open = false)">
        <span class="eyebrow">书架角色卡</span>
        <h2>新建对话默认项</h2>
        <p class="muted">
          用这张卡开始新故事时，以下用户角色和世界书会被预先选中，仍可在创建时修改。默认项不写入角色卡文件，也不影响已有对话。
        </p>
        <div v-if="loading" class="empty-small" role="status">正在读取默认项…</div>
        <div v-else-if="error && !defaults" class="card-sync-empty">
          <p role="alert">{{ error }}</p>
          <button @click="load"><Icon name="refresh" :size="15" />重试</button>
        </div>
        <template v-else>
          <label
            >用户角色<select v-model="personaId" :disabled="busy">
              <option value="">不预选（临时身份）</option>
              <option v-for="persona in personas" :key="persona.id" :value="persona.id">
                {{ persona.name }}
              </option>
            </select></label
          >
          <h3>世界书</h3>
          <p v-if="!worldbooks.length" class="empty-small">世界书库为空，可在左侧“世界书”页新建或导入。</p>
          <div v-else class="worldbook-choice-list">
            <label v-for="book in worldbooks" :key="book.id" class="check worldbook-choice">
              <input
                type="checkbox"
                :checked="chosen.has(book.id)"
                :disabled="busy"
                @change="toggle(book.id, ($event.target as HTMLInputElement).checked)"
              /><span>{{ book.name }}</span
              ><small>{{ book.entries }} 条</small>
            </label>
          </div>
        </template>
        <footer class="modal-footer card-sync-footer">
          <p v-if="error && defaults" class="card-sync-message" role="alert">{{ error }}</p>
          <button :disabled="busy" @click="open = false">取消</button>
          <button class="primary" :disabled="busy || loading || !changed" @click="save">
            {{ busy ? '正在保存…' : '保存默认项' }}
          </button>
        </footer>
      </AppModal>
    </div>
  </Teleport>
</template>
