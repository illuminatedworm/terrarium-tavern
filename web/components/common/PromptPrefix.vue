<script setup lang="ts">
defineProps<{ text: string; label: string }>();
</script>

<template>
  <section class="prompt-prefix" role="region" :aria-label="label">
    <span>系统预制头 · 只读</span>
    <pre>{{ text || '（未设置预制头）' }}</pre>
  </section>
</template>

<style scoped>
.prompt-prefix {
  min-width: 0;
  margin: 8px 0;
  padding: 9px 12px;
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  background: var(--surface-hover);
}
.prompt-prefix > span {
  display: block;
  margin-bottom: 5px;
  color: var(--text-muted);
  font-size: 11px;
}
.prompt-prefix > pre {
  max-height: 128px;
  overflow: auto;
  margin: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  color: var(--text-primary);
  font: inherit;
  font-size: 12px;
  line-height: 1.65;
}
</style>
