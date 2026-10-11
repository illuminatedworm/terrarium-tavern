<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { api } from '../../lib/api';
import {
  actionStatus,
  editorActions,
  mergeEditorEvents,
  type EditorAction,
  type EditorEvent,
} from '../../lib/cardAgentActivity';
import { activityText, type AgentActivity } from '../../lib/agentActivity';
import type { EditorRun } from '../../lib/cardEditor';
import AgentActivityItem from '../story/AgentActivityItem.vue';
import CardWorkspaceIcon from './CardWorkspaceIcon.vue';

const props = defineProps<{ run: EditorRun; events?: EditorEvent[] }>();
const emit = defineEmits<{ contentChange: [] }>();
const opened = ref(['queued', 'running'].includes(props.run.status));
const saved = ref<EditorEvent[]>([]);
const loaded = ref(false);
const loading = ref(false);
const error = ref('');
let alive = true;
const live = computed(() => ['queued', 'running'].includes(props.run.status));
const actions = computed(() =>
  editorActions(mergeEditorEvents(saved.value, props.events || []), props.run.status),
);
const toolCount = computed(() => actions.value.filter((item) => !item.id.startsWith('model-')).length);
const modelCount = computed(() => actions.value.length - toolCount.value);
const toolActions = computed(() => actions.value.filter((step) => !step.id.startsWith('model-')));

async function load() {
  if (loading.value || loaded.value) return;
  loading.value = true;
  error.value = '';
  let cursor = 0;
  try {
    do {
      const page = await api(`/card-edit-runs/${props.run.id}/activity?after=${cursor}`);
      if (!alive) return;
      saved.value = mergeEditorEvents(saved.value, page.events);
      if (!page.has_more) break;
      cursor = page.next_cursor;
    } while (alive);
    loaded.value = true;
  } catch (e: any) {
    if (alive) error.value = e.message;
  } finally {
    if (alive) {
      loading.value = false;
      emit('contentChange');
    }
  }
}
function toggle(event: Event) {
  if (event.target !== event.currentTarget) return;
  opened.value = (event.currentTarget as HTMLDetailsElement).open;
  if (opened.value) void load();
}
function item(step: EditorAction): AgentActivity {
  return {
    id: step.id,
    order: step.seq,
    kind: 'tool',
    summary: step.target,
    status:
      step.status === 'running'
        ? 'running'
        : step.status === 'completed'
          ? 'returned'
          : step.status === 'error'
            ? 'error'
            : 'interrupted',
    details: { arguments: activityText(step.arguments || ''), result: activityText(step.result || '') },
  };
}
watch(
  () => props.run.status,
  () => {
    loaded.value = false;
    if (props.run.status === 'completed') opened.value = false;
    if (opened.value) void load();
  },
);
if (opened.value) void load();
onBeforeUnmount(() => {
  alive = false;
});
</script>

<template>
  <details
    class="cw-run"
    :class="`run-${run.status}`"
    :open="opened"
    :data-run-id="run.id"
    aria-label="本回合工具与过程"
    @toggle="toggle"
  >
    <summary>
      <CardWorkspaceIcon name="chevron" /><span class="cw-run-title">工具与过程</span>
      <span v-if="loaded || actions.length" class="cw-run-count"
        >{{ toolCount }} 次工具调用<span v-if="modelCount"> · {{ modelCount }} 次请求</span></span
      >
      <span class="cw-run-status" :aria-live="live ? 'polite' : undefined">{{
        actionStatus(run.status)
      }}</span>
    </summary>
    <div v-if="opened" class="cw-run-body">
      <p v-if="run.error" class="run-error" role="alert">{{ run.error }}</p>
      <p v-if="loading" class="run-note" role="status">正在读取过程记录…</p>
      <p v-if="error" class="run-error" role="alert">{{ error }} <button @click="load">重试</button></p>
      <AgentActivityItem
        v-for="step in toolActions"
        :key="step.id"
        :item="item(step)"
        :title="step.title"
        :status-label="actionStatus(step.status)"
        endpoint=""
        :note="step.truncated ? '这里只展示日志摘要；模型读取的原文保持完整。' : undefined"
      />
      <details v-if="modelCount" class="run-requests">
        <summary>模型请求记录 · {{ modelCount }} 次</summary>
        <p
          v-for="step in actions.filter((entry) => entry.id.startsWith('model-'))"
          :key="step.id"
          class="run-model-call"
        >
          {{ step.target }}
        </p>
      </details>
      <p v-if="loaded && !actions.length" class="run-note">
        {{ live ? '等待模型或工具记录…' : '本回合没有工具调用记录。' }}
      </p>
    </div>
  </details>
</template>

<style scoped>
.cw-run {
  margin: 0 0 20px 52px;
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  padding: 0;
  background: transparent;
  font: 12px/1.6 var(--font-ui);
  min-width: 0;
}
.cw-run > summary {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 10px 12px;
  list-style: none;
  color: var(--text-secondary);
  cursor: pointer;
}
.cw-run > summary::-webkit-details-marker {
  display: none;
}
.cw-run > summary > svg {
  width: 13px;
  height: 13px;
  color: var(--text-muted);
}
.cw-run[open] > summary > svg {
  transform: rotate(90deg);
}
.cw-run-title {
  font-weight: 500;
  flex-shrink: 0;
}
.cw-run-count {
  color: var(--text-muted);
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cw-run-status {
  margin-left: auto;
  flex-shrink: 0;
  color: var(--text-muted);
  font-size: 11px;
}
.run-running .cw-run-status,
.run-queued .cw-run-status {
  color: var(--accent-hover);
}
.run-error .cw-run-status,
.run-cancelled .cw-run-status,
.run-interrupted .cw-run-status {
  color: var(--warning);
}
.cw-run-body {
  padding: 0 12px 12px;
  border-top: 1px solid var(--border-subtle);
}
.cw-run-body :deep(.agent-activity) {
  margin: 8px 0 0;
}
.run-model-call {
  margin: 12px 0 4px;
  font-size: 11px;
  color: var(--text-muted);
}
.run-requests {
  padding: 8px 0 0;
  margin: 0;
  border: 0;
  border-radius: 0;
  font-size: 11px;
  color: var(--text-muted);
}
.run-note {
  color: var(--text-muted);
  font-size: 12px;
  margin: 10px 0 0;
}
.run-error {
  color: var(--warning);
  overflow-wrap: anywhere;
}
@media (max-width: 620px) {
  .cw-run {
    margin-left: 0;
  }
  .cw-run-count > span {
    display: none;
  }
}
</style>
