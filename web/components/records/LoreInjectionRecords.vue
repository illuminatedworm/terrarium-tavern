<script setup lang="ts">
import type { LoreRecord } from '../../lib/loreRecords';
import RemoveLoreConfirm from '../story/RemoveLoreConfirm.vue';
defineProps<{ records: LoreRecord[]; removable?: boolean; disabled?: boolean }>();
const emit = defineEmits<{ remove: [record: LoreRecord] }>();
</script>

<template>
  <details class="lore-injection-records" :data-anchor-id="records[0]?.anchor_id">
    <summary>
      <span>世界书注入</span><small>{{ records.length }} 条</small>
    </summary>
    <div class="lore-injection-items">
      <details v-for="record in records" :key="record.id" class="lore-injection-item">
        <summary :title="record.title">
          <span>{{ record.title || `世界书条目 ${record.id}` }}</span>
          <RemoveLoreConfirm
            v-if="removable"
            :title="record.title"
            :disabled="disabled"
            @confirm="emit('remove', record)"
          />
        </summary>
        <pre>{{ record.content }}</pre>
      </details>
    </div>
  </details>
</template>

<style scoped>
.lore-injection-records {
  margin: 0 0 20px 52px;
  padding: 10px 0;
  border-block: 1px solid var(--border-subtle);
  font: 12px/1.7 var(--font-ui);
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
  font: 13px/1.8 var(--font-ui);
}
@media (max-width: 620px) {
  .lore-injection-records {
    margin-left: 0;
  }
}
</style>
