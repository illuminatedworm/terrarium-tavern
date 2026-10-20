<script setup lang="ts">
defineProps<{ text: string; label: string }>();
</script>

<template>
  <section v-if="text" class="prompt-prefix" role="region" :aria-label="label">
    <pre>{{ text }}</pre>
  </section>
</template>

<style scoped>
.prompt-prefix {
  position: relative;
  isolation: isolate;
  min-width: 0;
  padding: 5px 20px 5px 8px;
}
/* Two matching silhouettes keep a real diagonal border without clipping the text. */
.prompt-prefix::before,
.prompt-prefix::after {
  content: '';
  position: absolute;
  z-index: -1;
  clip-path: polygon(0 0, calc(100% - 12px) 0, 100% 100%, 0 100%);
}
.prompt-prefix::before {
  inset: 0;
  border-radius: var(--radius-xs) 0 0;
  background: var(--border-control);
}
.prompt-prefix::after {
  inset: 1px;
  border-radius: var(--radius-xs) 0 0;
  background: var(--surface-input);
}
.prompt-prefix > pre {
  max-height: 96px;
  overflow: auto;
  margin: 0;
  padding: 0;
  border-radius: 0;
  background: transparent;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  color: var(--text-secondary);
  font: inherit;
  font-size: var(--fs-xs);
  line-height: 1.5;
}
</style>
