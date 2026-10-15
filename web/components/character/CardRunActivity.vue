<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { api } from '../../lib/api';
import {
  actionStatus,
  editorActivityBlocks,
  mergeEditorEvents,
  type EditorAction,
  type EditorEvent,
} from '../../lib/cardAgentActivity';
import { activityText, type AgentActivity } from '../../lib/agentActivity';
import type { EditorRun } from '../../lib/cardEditor';
import AgentActivityItem from '../story/AgentActivityItem.vue';
import CardWorkspaceIcon from './CardWorkspaceIcon.vue';
import CardMessage from './CardMessage.vue';
import CardModelRequests from './CardModelRequests.vue';
import WebSources from '../common/WebSources.vue';
import { webSources } from '../../lib/webSearch';

const props = defineProps<{ run: EditorRun; events?: EditorEvent[] }>();
const emit = defineEmits<{ contentChange: [] }>();
const saved = ref<EditorEvent[]>([]);
const loaded = ref(false);
const loading = ref(false);
const error = ref('');
const opened = ref<Record<string, boolean>>({});
let alive = true;
const live = computed(() => ['queued', 'running'].includes(props.run.status));
const sources = computed(() =>
  webSources(
    mergeEditorEvents(saved.value, props.events || [])
      .filter((e) => e.kind === 'web_sources')
      .map((e) => ({ web_sources: e.payload.sources })),
  ),
);
const blocks = computed(() => {
  const result = editorActivityBlocks(mergeEditorEvents(saved.value, props.events || []), props.run.status);
  return result.length ? result : [{ kind: 'tools' as const, id: 'pending', actions: [] as EditorAction[] }];
});
const tools = (actions: EditorAction[]) => actions.filter((step) => !step.id.startsWith('model-'));
const requests = (actions: EditorAction[]) => actions.filter((step) => step.id.startsWith('model-'));

function toggleBlock(id: string, event: Event) {
  opened.value[id] = (event.currentTarget as HTMLDetailsElement).open;
  emit('contentChange');
}

async function load() {
  if (loading.value || loaded.value) return;
  loading.value = true;
  error.value = '';
  let cursor = 0;
  const status = props.run.status;
  try {
    do {
      const page = await api(`/card-edit-runs/${props.run.id}/activity?after=${cursor}`);
      if (!alive) return;
      saved.value = mergeEditorEvents(saved.value, page.events);
      if (!page.has_more) break;
      cursor = page.next_cursor;
    } while (alive);
    loaded.value = status === props.run.status;
  } catch (e: any) {
    if (alive) error.value = e.message;
  } finally {
    if (alive) {
      loading.value = false;
      emit('contentChange');
      if (!loaded.value && !error.value) void load();
    }
  }
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
    void load();
  },
);
void load();
onBeforeUnmount(() => {
  alive = false;
});
</script>

<template>
  <div class="cw-run-sequence" :data-run-id="run.id" aria-label="本回合工具与过程">
    <p v-if="loading" class="run-note" role="status">正在读取过程记录…</p>
    <p v-if="error" class="run-error" role="alert">{{ error }} <button @click="load">重试</button></p>
    <p v-if="run.error" class="run-error" role="alert">{{ run.error }}</p>
    <template v-for="block in blocks" :key="block.id">
      <CardMessage
        v-if="block.kind === 'assistant'"
        :text="block.text"
        :status="block.status"
        :data-activity-id="block.id"
      />
      <CardModelRequests
        v-else-if="!tools(block.actions).length && requests(block.actions).length"
        class="cw-request-only"
        :requests="requests(block.actions)"
      />
      <details
        v-else
        class="cw-run"
        :class="`run-${run.status}`"
        :open="opened[block.id] ?? live"
        @toggle="toggleBlock(block.id, $event)"
      >
        <summary>
          <CardWorkspaceIcon name="chevron" /><span class="cw-run-title">工具与过程</span>
          <span v-if="loaded || block.actions.length" class="cw-run-count">
            {{ tools(block.actions).length }} 项工具与联网活动
          </span>
          <span class="cw-run-status" :aria-live="live ? 'polite' : undefined">{{
            actionStatus(run.status)
          }}</span>
        </summary>
        <div class="cw-run-body">
          <AgentActivityItem
            v-for="step in tools(block.actions)"
            :key="step.id"
            :item="item(step)"
            :title="step.title"
            :status-label="actionStatus(step.status)"
            endpoint=""
            :note="step.truncated ? '这里只展示日志摘要；模型读取的原文保持完整。' : undefined"
          />
          <CardModelRequests
            v-if="requests(block.actions).length"
            class="run-requests"
            :requests="requests(block.actions)"
          />
          <p v-if="!block.actions.length" class="run-note">
            {{ live ? '等待模型或工具记录…' : '本回合没有工具调用记录。' }}
          </p>
        </div>
      </details>
    </template>
    <WebSources :sources="sources" />
  </div>
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
.cw-request-only {
  margin: 0 0 16px 52px;
}
.run-requests {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--border-subtle);
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
  .cw-run,
  .cw-request-only {
    margin-left: 0;
  }
}
</style>
