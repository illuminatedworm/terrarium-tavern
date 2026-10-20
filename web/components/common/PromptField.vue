<script setup lang="ts">
import PromptPrefix from './PromptPrefix.vue';

defineProps<{ prefix?: string; label: string }>();
</script>

<template>
  <div class="prompt-field" :class="{ 'has-prefix': !!prefix }">
    <PromptPrefix v-if="prefix" :text="prefix" :label="`${label}的系统预制头`" />
    <slot />
  </div>
</template>

<style scoped>
.prompt-field {
  display: grid;
  min-width: 0;
  min-height: 0;
}
.has-prefix {
  grid-template-rows: auto minmax(0, 1fr);
  border: 1px solid var(--border-control);
  border-radius: var(--radius-xs);
  background: var(--surface-base);
}
.has-prefix:focus-within {
  border-color: var(--focus-ring);
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.has-prefix > .prompt-prefix {
  justify-self: start;
  max-width: calc(100% + 2px);
  margin: -1px 0 0 -1px;
}
.has-prefix > :deep(input),
.has-prefix > :deep(textarea) {
  min-width: 0;
  min-height: 0;
  margin: 0;
  border: 0;
  border-radius: 0 0 var(--radius-xs) var(--radius-xs);
  background: transparent;
  outline: none;
}
</style>
