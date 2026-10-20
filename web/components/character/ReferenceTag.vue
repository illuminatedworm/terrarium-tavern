<script setup lang="ts">
import CardWorkspaceIcon from './CardWorkspaceIcon.vue';
defineProps<{ name: string; kind: 'card' | 'persona' | 'file'; disabled: boolean }>();
const labels = { card: '角色卡', persona: '用户角色', file: '文件' };
defineEmits<{ inspect: []; remove: [] }>();
</script>

<template>
  <span class="reference-tag" :title="`${labels[kind]}：${name}`">
    <button
      v-if="kind === 'card'"
      class="reference-content"
      type="button"
      :aria-label="`查看角色卡引用 ${name}`"
      @click="$emit('inspect')"
    >
      <CardWorkspaceIcon name="file" /><span>{{ name }}</span>
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
  position: relative;
  display: inline-flex;
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
  width: 100%;
  padding: 4px 20px 4px 8px;
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
  position: absolute;
  top: 1px;
  right: 1px;
  display: grid;
  place-items: center;
  width: 16px;
  height: 16px;
  padding: 1px;
  border: 0;
  border-radius: var(--radius-xs);
  background: var(--surface-panel);
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
    opacity: 1;
    pointer-events: auto;
  }
}
</style>
