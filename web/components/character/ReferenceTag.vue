<script setup lang="ts">
import CardWorkspaceIcon from './CardWorkspaceIcon.vue';
defineProps<{ name: string; kind: 'card' | 'worldbook' | 'persona' | 'file'; disabled: boolean }>();
const labels = { card: '角色卡', worldbook: '世界书', persona: '用户角色', file: '文件' };
defineEmits<{ inspect: []; remove: [] }>();
</script>

<template>
  <span class="reference-tag" :title="`${labels[kind]}：${name}`">
    <button
      v-if="kind === 'card' || kind === 'worldbook'"
      class="reference-content"
      type="button"
      :aria-label="`查看${labels[kind]}引用 ${name}`"
      @click="$emit('inspect')"
    >
      <CardWorkspaceIcon :name="kind === 'worldbook' ? 'book' : 'cards'" /><span>{{ name }}</span>
    </button>
    <span v-else class="reference-content"
      ><CardWorkspaceIcon :name="kind === 'file' ? 'file' : 'person'" /><span>{{ name }}</span></span
    >
    <button
      class="reference-remove"
      type="button"
      :disabled="disabled"
      :aria-label="`取消引用 ${labels[kind]} ${name}`"
      title="取消引用"
      @click.stop="$emit('remove')"
    >
      <CardWorkspaceIcon name="close" />
    </button>
  </span>
</template>

<style scoped>
.reference-tag {
  display: inline-flex;
  align-items: center;
  height: var(--cw-context-height, var(--control-h-sm));
  min-width: 0;
  max-width: min(100%, 260px);
  border: 1px solid var(--border-control);
  border-radius: var(--radius-xs);
  background: var(--surface-hover);
}
.reference-content {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  height: 100%;
  padding: 0 0 0 8px;
  border: 0;
  border-radius: inherit;
  background: transparent;
  color: var(--text-secondary);
  font: var(--fs-sm) / 1.5 var(--font-ui);
}
.reference-content > span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.reference-content svg {
  color: var(--accent-hover);
  width: 14px;
  height: 14px;
}
.reference-remove {
  display: grid;
  place-items: center;
  flex: none;
  width: 18px;
  height: calc(100% - 4px);
  margin-inline: 1px 2px;
  padding: 0;
  border: 0;
  border-radius: var(--radius-xs);
  background: transparent;
  color: var(--text-muted);
  opacity: 0;
  pointer-events: none;
}
.reference-remove svg {
  width: 12px;
  height: 12px;
}
.reference-tag:hover .reference-remove,
.reference-tag:focus-within .reference-remove {
  opacity: 1;
  pointer-events: auto;
}
.reference-remove:hover:not(:disabled) {
  color: var(--text-primary);
  background: var(--surface-hover);
}
@media (hover: none) {
  .reference-remove {
    width: var(--control-h-sm);
    opacity: 1;
    pointer-events: auto;
  }
}
</style>
