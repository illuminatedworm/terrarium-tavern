<script setup lang="ts">
import { computed, ref, shallowRef, watch } from 'vue';
import { activityText } from '../../lib/agentActivity';
import { api } from '../../lib/api';
import type { DialogueLoreRecord, LoreInjectionPayload } from '../../lib/loreRecords';
import RemoveLoreConfirm from '../story/RemoveLoreConfirm.vue';
const props = defineProps<{
  records: DialogueLoreRecord[];
  sessionId: string;
  previewChars?: number;
  showTitle: boolean;
  removable?: boolean;
  disabled?: boolean;
}>();
const emit = defineEmits<{ remove: [record: DialogueLoreRecord] }>();
const expanded = ref(false);
const openEntries = ref(new Set<string>());
const retry = ref(0);
const previews = shallowRef<Map<string, Map<string, LoreInjectionPayload>>>();
const error = ref('');
const items = computed(() =>
  props.records.map((record) => {
    const preview = previews.value?.get(record.source)?.get(record.id);
    const label = preview
      ? [preview.ref, preview.title].filter(Boolean).join(' · ')
      : props.showTitle && record.title;
    const formatted = preview && openEntries.value.has(record.id) ? activityText(preview) : undefined;
    return { record, preview, formatted, label: label || '世界书条目' };
  }),
);

function toggleEntry(id: string, event: Event) {
  if ((event.currentTarget as HTMLDetailsElement).open) openEntries.value.add(id);
  else openEntries.value.delete(id);
}

watch(
  [
    expanded,
    () => props.records,
    () => props.sessionId,
    () => props.previewChars,
    () => props.showTitle,
    retry,
  ],
  async (_, __, onCleanup) => {
    previews.value = undefined;
    error.value = '';
    if (!expanded.value) {
      openEntries.value.clear();
      return;
    }
    const controller = new AbortController();
    onCleanup(() => controller.abort());
    const records = props.records;
    const anchor = encodeURIComponent(records[0].anchor_id);
    const sources = new Set(records.map((record) => record.source));
    try {
      const groups = await Promise.all(
        [...sources].map(async (source) => {
          const rows: { id: string; payload: LoreInjectionPayload }[] = await api(
            `/sessions/${props.sessionId}/story/${source}/lore-previews?anchor_id=${anchor}&payload=true`,
            undefined,
            'GET',
            controller.signal,
          );
          return [source, new Map(rows.map((row) => [row.id, row.payload]))] as const;
        }),
      );
      if (!controller.signal.aborted) previews.value = new Map(groups);
    } catch (cause) {
      if (!controller.signal.aborted) error.value = String(cause instanceof Error ? cause.message : cause);
    }
  },
);
</script>

<template>
  <details
    class="lore-injection-records"
    :data-anchor-id="records[0]?.anchor_id"
    @toggle.self="expanded = ($event.currentTarget as HTMLDetailsElement).open"
  >
    <summary>
      <span>世界书注入</span><small>{{ records.length }} 条</small>
    </summary>
    <p v-if="error" role="alert">预览加载失败：{{ error }} <button @click="retry++">重试</button></p>
    <p v-else-if="expanded && !previews" role="status">正在读取世界书预览…</p>
    <div v-if="expanded" class="lore-injection-items">
      <details
        v-for="{ record, preview, formatted, label } in items"
        :key="record.id"
        class="lore-injection-item"
        @toggle.self="toggleEntry(record.id, $event)"
      >
        <summary>
          <span>{{ label }}</span>
          <small v-if="preview">{{ 'preview' in preview ? '开头预览' : '全文' }}</small>
          <RemoveLoreConfirm
            v-if="removable"
            :title="label"
            :disabled="disabled"
            @confirm="emit('remove', record)"
          />
        </summary>
        <p v-if="preview && preview.ref === null">旧记录未保存此条目的编号。</p>
        <pre v-if="formatted !== undefined">{{ formatted }}</pre>
        <p v-else-if="previews && !preview">该位置已无这条注入记录。</p>
      </details>
    </div>
  </details>
</template>

<style scoped>
.lore-injection-records {
  margin: 0 0 20px 52px;
  padding: 10px 0;
  border-block: 1px solid var(--border-subtle);
  font: var(--fs-sm) / 1.7 var(--font-ui);
}
.lore-injection-records > summary {
  display: flex;
  gap: 10px;
  align-items: center;
  list-style: none;
}
.lore-injection-records > summary::before {
  content: '›';
  color: var(--text-muted);
}
.lore-injection-records[open] > summary::before {
  content: '⌄';
}
.lore-injection-records > summary::-webkit-details-marker {
  display: none;
}
.lore-injection-records small {
  color: var(--text-muted);
  white-space: nowrap;
}
.lore-injection-items {
  margin-top: 8px;
}
.lore-injection-item {
  padding: 8px 0;
}
.lore-injection-item:last-child {
  border-bottom: 0;
}
.lore-injection-item > summary {
  display: flex;
  min-width: 0;
  gap: 8px;
  list-style: none;
}
.lore-injection-item > summary::before {
  content: '›';
  flex-shrink: 0;
}
.lore-injection-item[open] > summary::before {
  content: '⌄';
}
.lore-injection-item > summary::-webkit-details-marker {
  display: none;
}
.lore-injection-item > summary span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.lore-injection-item pre {
  margin: 10px 0 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  color: var(--text-secondary);
  font: var(--fs-sm) / 1.7 var(--font-mono);
}
@media (max-width: 620px) {
  .lore-injection-records {
    margin-left: 0;
  }
}
</style>
