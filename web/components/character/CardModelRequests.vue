<script setup lang="ts">
import type { EditorAction } from '../../lib/cardAgentActivity';

defineProps<{ requests: EditorAction[] }>();
</script>

<template>
  <ul class="cw-model-requests" aria-label="模型请求">
    <li v-for="request in requests" :key="request.id">
      <span class="request-label">{{ request.target }}</span>
      <span class="request-model">{{ request.model || '模型未记录' }}</span>
      <span v-if="request.searchFallback" class="request-note">联网降级重试</span>
    </li>
  </ul>
</template>

<style scoped>
.cw-model-requests {
  list-style: none;
  padding: 0;
  margin: 0;
  color: var(--text-muted);
  font: 11px/1.6 var(--font-ui);
}
.cw-model-requests li {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 4px 12px;
  padding: 3px 0;
}
.request-label {
  flex-shrink: 0;
}
.request-model {
  color: var(--text-secondary);
  overflow-wrap: anywhere;
  min-width: 0;
}
.request-note {
  color: var(--warning);
}
</style>
