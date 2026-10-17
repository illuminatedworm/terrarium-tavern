<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import ExpandableTextarea from '../common/ExpandableTextarea.vue';
import Icon from '../common/Icon.vue';
import StoryProse from '../story/StoryProse.vue';

const props = defineProps<{
  memory: any;
  disabled?: boolean;
  saving?: boolean;
  /** `cards` is the dialog layout: one card per record with its source rounds. */
  layout?: 'list' | 'cards';
  /** Message id → 1-based round (0 for the opening), used for source labels. */
  rounds?: Map<string, number>;
}>();
const emit = defineEmits<{ change: [value: any]; locate: [id: string, excerpt: string] }>();
const categories: Record<string, string> = {
  current: '当前进展',
  episode: '变化与经历',
  open: '未完成事项',
  uncertain: '待确认内容',
  manual: '手工记忆',
};
const editable = Object.entries(categories);
const search = ref('');
const openGroups = ref(new Set<string>());
const allGroups = computed(() =>
  Object.entries(categories)
    .map(([id, label]) => ({
      id,
      label,
      records: (props.memory?.records || []).filter(
        (r: any) => r.category === id && !['deleted', 'superseded'].includes(r.status),
      ),
    }))
    .filter((g) => g.records.length),
);
const groups = computed(() => {
  const query = search.value.trim().toLocaleLowerCase();
  if (!query) return allGroups.value;
  return allGroups.value
    .map((group) => ({
      ...group,
      records: group.records.filter((record: any) =>
        [group.label, record.title, record.content, record.story_time, ...(record.people || [])]
          .filter(Boolean)
          .join('\n')
          .toLocaleLowerCase()
          .includes(query),
      ),
    }))
    .filter((group) => group.records.length);
});
const draft = ref<any>(null);
watch(search, (value) => {
  if (value.trim()) openGroups.value = new Set(groups.value.map((group) => group.id));
});
watch(
  () => props.memory,
  () => {
    draft.value = null;
  },
);
function edit(record?: any) {
  draft.value = {
    action: record ? 'edit' : 'add',
    id: record?.id,
    category: record?.category || 'manual',
    kind: record?.kind || 'fact',
    content: record?.content || '',
  };
}
const cards = computed(() => groups.value.flatMap((group) => group.records));
const openSources = ref(new Set<string>());
function toggleSources(id: string) {
  const next = new Set(openSources.value);
  next.has(id) ? next.delete(id) : next.add(id);
  openSources.value = next;
}
function roundName(round: number) {
  return round ? `第 ${round} 轮` : '开场';
}
function sourceLabel(record: any) {
  const rounds = record.sources
    .map((source: any) => props.rounds?.get(source.message_id))
    .filter((round: number | undefined) => round != null) as number[];
  if (!rounds.length) return `${record.sources.length} 处`;
  const first = Math.min(...rounds),
    last = Math.max(...rounds);
  if (first === last) return roundName(first);
  return first ? `第 ${first}–${last} 轮` : `开场–${roundName(last)}`;
}
function toggleGroup(id: string) {
  const next = new Set(openGroups.value);
  next.has(id) ? next.delete(id) : next.add(id);
  openGroups.value = next;
}
</script>

<template>
  <div class="classified-memory" :class="{ 'memory-cards': layout === 'cards' }">
    <div class="memory-records-scroll">
      <p v-if="layout !== 'cards'" class="muted memory-help">记录剧情发展，不直接修改变量。</p>
      <form v-if="draft" class="memory-editor" @submit.prevent="emit('change', { ...draft })">
        <label
          >分类<select v-model="draft.category" :disabled="saving">
            <option v-for="[key, label] in editable" :key="key" :value="key">{{ label }}</option>
          </select></label
        >
        <label
          >内容性质<select v-model="draft.kind" :disabled="saving">
            <option value="fact">事实记录</option>
            <option value="claim">人物说法</option>
            <option value="inference">推断</option>
          </select></label
        >
        <ExpandableTextarea v-model="draft.content" label="记忆条目内容" />
        <p class="muted">
          保存后标记为手工修订，自动整理不会覆盖或删除；原有引用只表示来源，不证明你的修订内容。
        </p>
        <div class="memory-actions">
          <button type="submit" :disabled="saving || disabled || !draft.content.trim()">
            {{ saving ? '保存中…' : '保存条目' }}</button
          ><button type="button" :disabled="saving" @click="draft = null">取消编辑</button>
        </div>
      </form>
      <template v-else-if="layout === 'cards'">
        <div class="memory-toolbar">
          <input
            v-model="search"
            type="search"
            aria-label="搜索记忆条目"
            placeholder="搜索记忆条目…"
          /><button :disabled="disabled || saving" @click="edit()">
            <Icon name="plus" :size="15" />添加记忆条目
          </button>
        </div>
        <article
          v-for="record in cards"
          :key="record.id"
          class="memory-card"
          :class="`memory-${record.category}`"
        >
          <header>
            <span class="memory-card-category">{{ categories[record.category] }}</span
            ><span v-if="record.manual" class="memory-chip">手工修订</span
            ><span v-if="record.status === 'completed'" class="memory-chip">已完成</span
            ><span v-if="record.kind === 'claim'" class="memory-chip">人物说法</span
            ><span v-if="record.kind === 'inference'" class="memory-chip">推断</span
            ><span v-if="record.source_stale" class="memory-chip warn">原文已修改，尚未重建</span>
            <span class="memory-card-actions"
              ><button
                v-if="record.sources?.length"
                class="memory-source-pill"
                :aria-expanded="openSources.has(record.id)"
                :title="`原文依据 · ${record.sources.length} 处`"
                @click="toggleSources(record.id)"
              >
                来源 · {{ sourceLabel(record) }}</button
              ><button
                class="icon-button"
                aria-label="编辑条目"
                title="编辑条目"
                :disabled="disabled || saving"
                @click="edit(record)"
              >
                <Icon name="pencil" :size="14" /></button
              ><button
                class="icon-button"
                aria-label="删除条目"
                title="删除条目"
                :disabled="disabled || saving"
                @click="emit('change', { action: 'delete', id: record.id })"
              >
                <Icon name="trash" :size="14" /></button
            ></span>
          </header>
          <StoryProse :text="record.content" />
          <p
            v-if="(record.story_time && record.story_time !== '未知') || record.known_to?.length"
            class="memory-card-meta"
          >
            <span v-if="record.story_time && record.story_time !== '未知'">时间：{{ record.story_time }}</span
            ><span v-if="record.known_to?.length">明确知情：{{ record.known_to.join('、') }}</span>
          </p>
          <div v-if="openSources.has(record.id)" class="memory-card-sources">
            <div v-for="(source, i) in record.sources" :key="i" class="memory-source">
              <blockquote>{{ source.excerpt }}</blockquote>
              <p v-if="source.state !== 'valid'" class="muted">
                {{
                  source.state === 'missing'
                    ? '来源不在当前分支'
                    : source.state === 'changed'
                      ? '原文已修改，摘录尚未重建'
                      : '来源缺少指纹，尚未核验'
                }}
              </p>
              <button
                :disabled="source.state === 'missing'"
                @click="emit('locate', source.message_id, source.state === 'valid' ? source.excerpt : '')"
              >
                定位原文
              </button>
            </div>
          </div>
        </article>
        <p v-if="!allGroups.length" class="state-empty">暂无记忆记录</p>
        <p v-else-if="!groups.length" class="state-empty">没有匹配的记忆条目</p>
        <p class="memory-help memory-cards-help">
          每条记忆都能回查来源，也可以手工修订。删除保留屏蔽标记，自动整理不会恢复同一条目；修改旧剧情只提示，需手动重新生成记忆才能核对旧内容。
        </p>
      </template>
      <template v-else>
        <details
          v-for="group in groups"
          :key="group.id"
          class="memory-group"
          :open="openGroups.has(group.id)"
        >
          <summary @click.prevent="toggleGroup(group.id)">
            <span>{{ group.label }}</span> <small>{{ group.records.length }} 条</small>
          </summary>
          <article v-for="record in group.records" :key="record.id" class="memory-record">
            <div class="memory-tags">
              <span v-if="record.manual">手工修订</span
              ><span v-if="record.status === 'completed'">已完成</span
              ><span v-if="record.kind === 'claim'">人物说法</span
              ><span v-if="record.kind === 'inference'">推断</span
              ><span v-if="record.source_stale">原文已修改，尚未重建</span>
            </div>
            <StoryProse :text="record.content" />
            <p v-if="record.story_time && record.story_time !== '未知'" class="muted">
              时间：{{ record.story_time }}
            </p>
            <p v-if="record.known_to?.length" class="muted">明确知情：{{ record.known_to.join('、') }}</p>
            <details v-if="record.sources?.length">
              <summary>原文依据 · {{ record.sources.length }} 处</summary>
              <div v-for="(source, i) in record.sources" :key="i" class="memory-source">
                <blockquote>{{ source.excerpt }}</blockquote>
                <p v-if="source.state !== 'valid'" class="muted">
                  {{
                    source.state === 'missing'
                      ? '来源不在当前分支'
                      : source.state === 'changed'
                        ? '原文已修改，摘录尚未重建'
                        : '来源缺少指纹，尚未核验'
                  }}
                </p>
                <button
                  :disabled="source.state === 'missing'"
                  @click="emit('locate', source.message_id, source.state === 'valid' ? source.excerpt : '')"
                >
                  定位原文
                </button>
              </div>
            </details>
            <div class="memory-actions">
              <button :disabled="disabled || saving" @click="edit(record)">编辑条目</button
              ><button
                :disabled="disabled || saving"
                @click="emit('change', { action: 'delete', id: record.id })"
              >
                删除条目
              </button>
            </div>
          </article>
        </details>
        <p v-if="!allGroups.length" class="state-empty">暂无记忆记录</p>
        <p v-else-if="!groups.length" class="state-empty">没有匹配的记忆条目</p>
        <button :disabled="disabled || saving" @click="edit()">添加记忆条目</button>
        <p class="muted memory-help">
          删除保留屏蔽标记，自动整理不会恢复同一条目。修改旧剧情只提示，需手动重新生成记忆才能核对旧内容。
        </p>
      </template>
    </div>
    <div v-if="layout !== 'cards'" class="memory-search">
      <input v-model="search" type="search" aria-label="搜索记忆条目" placeholder="搜索记忆条目…" />
    </div>
  </div>
</template>

<style scoped>
.classified-memory {
  min-width: 0;
  overflow-wrap: anywhere;
}
.memory-records-scroll {
  min-height: 0;
}
.memory-search {
  position: sticky;
  z-index: 2;
  bottom: 0;
  flex: none;
  padding: 10px 0 2px;
  border-top: 1px solid var(--border-subtle);
  background: var(--surface-sidebar);
}
.memory-search input {
  width: 100%;
  min-height: 40px;
  margin: 0;
  font-size: 13px;
}
.memory-help,
.memory-source p {
  font-size: 0.82rem;
  line-height: 1.6;
}
.memory-group {
  margin: 0;
  border-bottom: 1px solid var(--border-subtle);
}
.memory-group > summary {
  cursor: pointer;
  color: var(--text-secondary);
  font-weight: 600;
  margin-bottom: 0;
  min-height: 44px;
  padding: 11px 2px;
  font-size: 13px;
}
.memory-group small {
  float: right;
  color: var(--text-muted);
  font-weight: normal;
  font-size: 11px;
}
.memory-record {
  border-bottom: 1px solid var(--border-subtle);
  padding: 0.7rem 0;
  min-width: 0;
}
.memory-tags,
.memory-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  margin: 0.4rem 0;
}
.memory-tags {
  color: var(--text-muted);
  font-size: 0.78rem;
}
.memory-records-scroll > button {
  min-height: 38px;
  margin-top: 14px;
  font-size: 12.5px;
}
.memory-actions button,
.memory-source button {
  font-size: 0.8rem;
  padding: 0.3rem 0.6rem;
}
.memory-editor {
  display: grid;
  gap: 0.6rem;
}
.memory-source blockquote {
  margin: 0.5rem 0;
  white-space: pre-wrap;
}
/* ---------- Dialog layout: one card per record ---------- */
.memory-toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 14px;
}
.memory-toolbar input {
  flex: 1;
  min-width: 0;
  margin: 0;
}
.memory-toolbar button {
  flex: none;
}
.memory-card {
  --memory-color: var(--ctx-memory);
  margin-bottom: 12px;
  padding: 14px 18px 16px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: rgb(var(--well-rgb) / 0.35);
  transition: border-color var(--dur);
}
.memory-card:hover,
.memory-card:focus-within {
  border-color: color-mix(in srgb, var(--memory-color) 35%, var(--border-control));
}
.memory-open {
  --memory-color: var(--ctx-turn);
}
.memory-episode {
  --memory-color: var(--ctx-history);
}
.memory-uncertain {
  --memory-color: var(--ctx-rules);
}
.memory-manual {
  --memory-color: var(--ctx-character);
}
.memory-card header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
}
.memory-card-category {
  margin-right: 4px;
  color: var(--memory-color);
  font-size: var(--fs-sm);
  font-weight: 500;
  letter-spacing: 0.02em;
}
.memory-chip {
  padding: 1px 8px;
  border-radius: var(--radius-pill);
  background: rgb(255 255 255 / 0.05);
  color: var(--text-muted);
  font-size: var(--fs-2xs);
}
.memory-chip.warn {
  color: var(--warning);
}
.memory-card-actions {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-left: auto;
}
.memory-source-pill {
  min-height: 24px;
  padding: 1px 10px;
  border: 1px solid var(--border-control);
  border-radius: var(--radius-pill);
  background: transparent;
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
}
.memory-source-pill[aria-expanded='true'] {
  border-color: color-mix(in srgb, var(--memory-color) 50%, transparent);
  color: var(--text-primary);
}
.memory-card header .icon-button {
  width: 28px;
  height: 28px;
  min-height: 0;
}
.memory-card :deep(.prose) {
  color: var(--text-primary);
  font: var(--fs-base) / 1.85 var(--font-ui);
}
.memory-card-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 16px;
  margin: 8px 0 0;
  color: var(--text-muted);
  font-size: var(--fs-xs);
}
.memory-card-sources {
  display: grid;
  gap: 8px;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border-subtle);
}
.memory-card-sources .memory-source {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 10px;
}
.memory-card-sources blockquote {
  flex: 1 1 240px;
  margin: 0;
  padding-left: 10px;
  border-left: 2px solid color-mix(in srgb, var(--memory-color) 45%, transparent);
  color: var(--text-secondary);
  font-size: var(--fs-sm);
  line-height: 1.7;
}
.memory-card-sources .memory-source p {
  flex-basis: 100%;
  margin: 0;
}
.memory-cards-help {
  margin-top: 16px;
  color: var(--text-muted);
}
</style>
