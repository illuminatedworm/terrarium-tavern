<script setup lang="ts">
import AppModal from '../common/AppModal.vue';
import Icon from '../common/Icon.vue';
import { computed, onMounted, ref } from 'vue';
import { api } from '../../lib/api';

interface SyncItem {
  key: string;
  group: 'character' | 'lore' | 'regex' | 'scripts';
  label: string;
  change: 'add' | 'update' | 'remove';
  origin: 'card' | 'story' | 'both';
  before: unknown;
  after: unknown;
}
const props = defineProps<{ session: any; blocked: string }>();
const emit = defineEmits<{ close: []; synced: [session: any] }>();

const groups = [
  { id: 'character', title: '角色设定', description: '提示词中的人物字段。' },
  { id: 'lore', title: '世界书条目', description: '按条目 ID 对应；删除只移除对话中多出的条目。' },
  { id: 'regex', title: '正则脚本', description: '按脚本 ID 对应；执行顺序单独列出。' },
  { id: 'scripts', title: '角色脚本', description: 'Tavern Helper 脚本整体同步。' },
] as const;
const fieldNames: Record<string, string> = {
  name: '角色名称',
  nickname: '昵称',
  description: '角色描述',
  personality: '性格',
  scenario: '场景',
  system_prompt: '角色系统提示',
  post_history_instructions: '历史后提示',
  mes_example: '示例对话',
};
const changes = { add: '新增', update: '覆盖', remove: '删除' };
const origins = {
  card: { label: '书架卡更新', help: '只有书架角色卡改过，同步不会丢失对话内的变化。' },
  story: { label: '对话内改动', help: '只有对话改过（Tracker、脚本或手工编辑），同步会还原为书架卡内容。' },
  both: { label: '双方都改过', help: '书架卡和对话都与开局时不同，同步会以书架卡为准。' },
};

const card = ref<{ id: string; name: string; revision: number } | null>(null);
const items = ref<SyncItem[]>([]);
const selected = ref(new Set<string>());
const expanded = ref(new Set<string>());
const query = ref('');
const loading = ref(true),
  busy = ref(false),
  error = ref('');

const visible = computed(() => {
  const text = query.value.trim().toLowerCase();
  return text ? items.value.filter((item) => itemLabel(item).toLowerCase().includes(text)) : items.value;
});
const sections = computed(() =>
  groups
    .map((group) => ({ ...group, items: visible.value.filter((item) => item.group === group.id) }))
    .filter((group) => group.items.length),
);

function itemLabel(item: SyncItem) {
  return item.group === 'character' ? fieldNames[item.label] || item.label : item.label;
}
function preview(value: unknown) {
  if (value === null || value === undefined) return '（无）';
  return typeof value === 'string' ? value || '（空）' : JSON.stringify(value, null, 2);
}
function choose(predicate: (item: SyncItem) => boolean) {
  selected.value = new Set(items.value.filter(predicate).map((item) => item.key));
}
function toggle(key: string, on: boolean) {
  const next = new Set(selected.value);
  if (on) next.add(key);
  else next.delete(key);
  selected.value = next;
}
function groupState(list: SyncItem[]) {
  const count = list.filter((item) => selected.value.has(item.key)).length;
  return { all: count === list.length, some: count > 0 && count < list.length };
}
function toggleGroup(list: SyncItem[], on: boolean) {
  const next = new Set(selected.value);
  for (const item of list) {
    if (on) next.add(item.key);
    else next.delete(item.key);
  }
  selected.value = next;
}
function toggleExpanded(key: string, open: boolean) {
  // Previews render only while open so long worldbooks stay responsive.
  const next = new Set(expanded.value);
  if (open) next.add(key);
  else next.delete(key);
  expanded.value = next;
}

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const result = await api(`/sessions/${props.session.id}/card-sync`);
    card.value = result.card;
    items.value = result.items;
    expanded.value = new Set();
    choose((item) => item.origin === 'card');
  } catch (e: any) {
    error.value = e.message;
  } finally {
    loading.value = false;
  }
}
async function submit() {
  if (!card.value || !selected.value.size || props.blocked) return;
  busy.value = true;
  error.value = '';
  try {
    const updated = await api(`/sessions/${props.session.id}/card-sync`, {
      keys: [...selected.value],
      card_revision: card.value.revision,
      expected_revision: props.session.revision,
      operation_id: crypto.randomUUID(),
    });
    emit('synced', updated);
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
      <AppModal label="从书架角色卡同步" size="large" class="card-sync-modal" @close="!busy && emit('close')">
        <span class="eyebrow">对话级角色卡</span>
        <h2>从书架角色卡同步{{ card ? `：${card.name}` : '' }}</h2>
        <p class="muted">
          对比书架角色卡与当前对话使用的角色卡，只列出故事中仍会读取的内容；开场白、作者备注、标签和世界书整体设置只在开局或展示时使用，不参与同步。默认勾选只在书架卡上改过的项目；“对话内改动”和“双方都改过”会以书架卡覆盖对话中的变化。
        </p>
        <div v-if="loading" class="empty-small" role="status">正在比较书架角色卡…</div>
        <div v-else-if="error && !items.length" class="card-sync-empty">
          <p role="alert">{{ error }}</p>
          <button @click="load"><Icon name="refresh" :size="15" />重试</button>
        </div>
        <p v-else-if="!items.length" class="empty-small">当前对话与书架角色卡一致，没有需要同步的内容。</p>
        <template v-else>
          <div class="card-sync-toolbar">
            <div class="toolbar">
              <button :disabled="busy" @click="choose((item) => item.origin === 'card')">仅书架卡更新</button>
              <button :disabled="busy" @click="choose(() => true)">全部覆盖</button>
              <button :disabled="busy" @click="choose(() => false)">全部取消</button>
            </div>
            <input v-model="query" type="search" placeholder="按名称筛选" aria-label="按名称筛选同步项" />
            <span class="muted">已选 {{ selected.size }} / {{ items.length }}</span>
          </div>
          <p v-if="!sections.length" class="empty-small">没有名称匹配的同步项。</p>
          <section v-for="section in sections" :key="section.id" class="card-sync-group">
            <label class="check card-sync-group-heading">
              <input
                type="checkbox"
                :checked="groupState(section.items).all"
                :indeterminate="groupState(section.items).some"
                :disabled="busy"
                @change="toggleGroup(section.items, ($event.target as HTMLInputElement).checked)"
              />
              <strong>{{ section.title }}</strong>
              <small>{{ section.items.length }}</small>
              <span class="muted">{{ section.description }}</span>
            </label>
            <div v-for="item in section.items" :key="item.key" class="card-sync-item">
              <input
                type="checkbox"
                :checked="selected.has(item.key)"
                :disabled="busy"
                :aria-label="`同步 ${itemLabel(item)}`"
                @change="toggle(item.key, ($event.target as HTMLInputElement).checked)"
              />
              <details @toggle="toggleExpanded(item.key, ($event.target as HTMLDetailsElement).open)">
                <summary>
                  <span class="card-sync-label">{{ itemLabel(item) }}</span>
                  <span class="badge">{{ changes[item.change] }}</span>
                  <span
                    class="card-sync-origin"
                    :class="`origin-${item.origin}`"
                    :title="origins[item.origin].help"
                    >{{ origins[item.origin].label }}</span
                  >
                </summary>
                <div v-if="expanded.has(item.key)" class="card-sync-compare">
                  <div>
                    <h4>当前对话</h4>
                    <pre>{{ preview(item.before) }}</pre>
                  </div>
                  <div>
                    <h4>书架角色卡</h4>
                    <pre>{{ preview(item.after) }}</pre>
                  </div>
                </div>
              </details>
            </div>
          </section>
        </template>
        <footer class="modal-footer card-sync-footer">
          <p v-if="error && items.length" class="card-sync-message" role="alert">{{ error }}</p>
          <p v-else-if="blocked" class="card-sync-message">{{ blocked }}</p>
          <button :disabled="busy" @click="emit('close')">取消</button>
          <button class="primary" :disabled="busy || loading || !!blocked || !selected.size" @click="submit">
            {{ busy ? '正在同步…' : `同步所选 ${selected.size} 项` }}
          </button>
        </footer>
      </AppModal>
    </div>
  </Teleport>
</template>
