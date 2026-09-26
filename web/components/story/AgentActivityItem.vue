<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { api } from '../../lib/api';
import { toolLabels, type AgentActivity } from '../../lib/agentActivity';

const props = defineProps<{ item: AgentActivity; endpoint: string }>();
type Part = 'arguments' | 'result' | 'content';
type Page = { text: string; next: number | null; total: number };
const opened = ref(false);
const pages = ref<Partial<Record<Part, Page>>>({});
const errors = ref<Partial<Record<Part, string>>>({});
const loading = ref(new Set<Part>());
let generation = 0;
const parts = computed<Part[]>(() => (props.item.kind === 'tool' ? ['arguments', 'result'] : ['content']));
const title = computed(() =>
  props.item.kind === 'assistant' ? 'Agent 回复' : toolLabels[props.item.name || ''] || props.item.name,
);
const labels = { arguments: '调用参数', result: '工具返回', content: '回复内容' };
const status = computed(
  () =>
    ({ running: '执行中', returned: '已返回', error: '返回错误', interrupted: '已中断' })[props.item.status],
);

async function load(part: Part, more = false) {
  if (loading.value.has(part) || (!more && pages.value[part])) return;
  if (part === 'result' && props.item.status === 'running') return;
  const version = generation;
  const offset = more ? (pages.value[part]?.next ?? 0) : 0;
  loading.value.add(part);
  delete errors.value[part];
  try {
    const local = props.item.details?.[part];
    let page: Page;
    if (local !== undefined) {
      const end = Math.min(local.length, offset + 12000);
      page = { text: local.slice(offset, end), next: end < local.length ? end : null, total: local.length };
    } else {
      page = await api(
        `${props.endpoint}/${encodeURIComponent(props.item.id)}?part=${part}&offset=${offset}`,
      );
    }
    if (version !== generation) return;
    pages.value[part] = { ...page, text: (more ? pages.value[part]?.text || '' : '') + page.text };
  } catch (error: any) {
    if (version === generation) errors.value[part] = error.message;
  } finally {
    if (version === generation) loading.value.delete(part);
  }
}
function toggle(event: Event) {
  opened.value = (event.currentTarget as HTMLDetailsElement).open;
  if (opened.value) for (const part of parts.value) void load(part);
}
watch(
  () => [props.endpoint, props.item.id],
  () => {
    generation++;
    pages.value = {};
    errors.value = {};
    loading.value.clear();
    if (opened.value) for (const part of parts.value) void load(part);
  },
);
watch(
  () => props.item.status,
  () => {
    if (opened.value) void load('result');
  },
);
onBeforeUnmount(() => {
  generation++;
});
</script>

<template>
  <details class="agent-activity" :class="item.status" :data-activity-id="item.id" @toggle="toggle">
    <summary>
      <span class="activity-title">{{ title }}</span>
      <code v-if="item.name" class="activity-name">{{ item.name }}</code>
      <span class="activity-preview" :title="item.summary">{{ item.summary }}</span>
      <span
        v-if="item.kind === 'tool'"
        class="activity-status"
        :aria-live="item.status === 'running' ? 'polite' : undefined"
        >{{ status }}</span
      >
    </summary>
    <div v-if="opened" class="activity-details">
      <section v-for="part in parts" :key="part" class="activity-part" :aria-label="labels[part]">
        <h3>{{ labels[part] }}</h3>
        <p v-if="part === 'result' && item.status === 'running'" class="muted">等待工具返回…</p>
        <template v-else>
          <pre v-if="pages[part]">{{ pages[part]!.text || '（空）' }}</pre>
          <p v-if="loading.has(part)" class="muted">读取中…</p>
          <p v-if="errors[part]" role="alert">
            {{ errors[part] }} <button @click="load(part, !!pages[part])">重试</button>
          </p>
          <button v-if="pages[part]?.next != null" :disabled="loading.has(part)" @click="load(part, true)">
            继续读取{{ labels[part] }}（已显示 {{ pages[part]!.text.length }} /
            {{ pages[part]!.total }} 字符）
          </button>
        </template>
      </section>
    </div>
  </details>
</template>

<style scoped>
.agent-activity {
  margin: 8px 0 12px 52px;
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  font: 12px/1.6 var(--font-ui);
  color: var(--text-secondary);
  min-width: 0;
}
.agent-activity > summary {
  display: flex;
  gap: 9px;
  align-items: center;
  padding: 9px 12px;
  cursor: pointer;
  list-style: none;
  min-width: 0;
}
.agent-activity > summary::-webkit-details-marker {
  display: none;
}
.agent-activity > summary::before {
  content: '›';
  color: var(--text-muted);
  flex-shrink: 0;
}
.agent-activity[open] > summary::before {
  content: '⌄';
}
.agent-activity > summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: 8px;
}
.activity-title {
  flex-shrink: 0;
  font-weight: 600;
}
.activity-name {
  color: var(--text-muted);
  font-size: 11px;
  overflow-wrap: anywhere;
}
.activity-preview {
  white-space: nowrap;
  text-overflow: ellipsis;
  overflow: hidden;
  flex: 1;
  min-width: 0;
  color: var(--text-muted);
}
.activity-status {
  margin-left: auto;
  white-space: nowrap;
  color: var(--text-muted);
}
.running .activity-status {
  color: var(--accent);
}
.error .activity-status,
.interrupted .activity-status {
  color: var(--text-primary);
}
.activity-details {
  padding: 0 14px 14px;
  border-top: 1px solid var(--border-subtle);
}
.activity-part h3 {
  margin: 12px 0 7px;
  font: 600 12px var(--font-ui);
  color: var(--text-secondary);
}
.activity-part pre {
  margin: 0;
  max-height: 340px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font: 12px/1.7 monospace;
  color: var(--text-secondary);
}
.activity-part button {
  margin-top: 8px;
  font-size: 12px;
}
@media (max-width: 620px) {
  .agent-activity {
    margin-left: 0;
  }
  .activity-name {
    display: none;
  }
  .agent-activity > summary {
    gap: 7px;
    padding: 9px;
  }
}
</style>
