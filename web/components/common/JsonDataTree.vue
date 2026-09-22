<script setup lang="ts">
import { computed, ref } from 'vue';
const props = defineProps<{ value: any }>();
const opened = ref(new Set<string>());
const entries = computed(() =>
  props.value !== null && typeof props.value === 'object' ? Object.entries(props.value) : [],
);
function describe(value: any) {
  if (value === null) return 'null';
  if (Array.isArray(value)) return `数组 · ${value.length} 项`;
  if (typeof value === 'object') return `对象 · ${Object.keys(value).length} 项`;
  if (typeof value === 'string') return `文本 · ${value.length} 字符`;
  return String(value);
}
function toggle(key: string, event: Event) {
  if ((event.target as HTMLDetailsElement).open) opened.value.add(key);
  else opened.value.delete(key);
}
</script>
<template>
  <div class="json-data-tree">
    <details v-for="[key, entry] in entries" :key="key" @toggle="toggle(key, $event)">
      <summary>
        <span class="json-key">{{ key }}</span
        ><small>{{ describe(entry) }}</small>
      </summary>
      <template v-if="opened.has(key)">
        <JsonDataTree
          v-if="entry !== null && typeof entry === 'object' && Object.keys(entry).length"
          :value="entry"
        />
        <pre v-else>{{ typeof entry === 'string' ? entry : JSON.stringify(entry, null, 2) }}</pre>
      </template>
    </details>
    <p v-if="!entries.length" class="muted">{{ JSON.stringify(value) }}</p>
  </div>
</template>
<style scoped>
.json-data-tree {
  min-width: 0;
  font-size: 12px;
}
details {
  border: 0;
  border-bottom: 1px solid var(--border-subtle);
  border-radius: 0;
  padding: 0;
  margin: 0;
}
summary {
  padding: 10px 0;
  overflow-wrap: anywhere;
}
.json-key {
  font-family: Consolas, monospace;
  color: var(--text-primary);
}
small {
  color: var(--text-muted);
  margin-left: 8px;
  font-size: 10px;
}
.json-data-tree .json-data-tree {
  margin: 0 0 8px 9px;
  padding-left: 10px;
  border-left: 1px solid var(--border-control);
}
pre {
  margin: 0 0 12px;
  padding: 10px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 360px;
  overflow: auto;
  font-size: 12px;
}
</style>
