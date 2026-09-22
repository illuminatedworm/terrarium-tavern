<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import ExpandableTextarea from '../common/ExpandableTextarea.vue';
import StoryProse from '../story/StoryProse.vue';

const props = defineProps<{ memory: any; disabled?: boolean; saving?: boolean }>();
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
function toggleGroup(id: string) {
  const next = new Set(openGroups.value);
  next.has(id) ? next.delete(id) : next.add(id);
  openGroups.value = next;
}
</script>

<template>
  <div class="classified-memory">
    <div class="memory-records-scroll">
      <p class="muted memory-help">记录剧情发展，不直接修改变量。</p>
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
    <div class="memory-search">
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
  border-top: 1px solid var(--line);
  background: var(--surface-sidebar);
}
.memory-search input {
  width: 100%;
  margin: 0;
}
.memory-help,
.memory-source p {
  font-size: 0.82rem;
  line-height: 1.6;
}
.memory-group {
  margin-block: 1rem;
}
.memory-group > summary {
  cursor: pointer;
  color: var(--text-secondary);
  font-weight: 600;
  margin: 0 0 0.5rem;
}
.memory-group:not([open]) > summary {
  margin-bottom: 0;
}
.memory-group small {
  color: var(--muted);
  font-weight: normal;
}
.memory-record {
  border-bottom: 1px solid var(--line);
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
  color: var(--muted);
  font-size: 0.78rem;
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
</style>
