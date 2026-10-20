<script setup lang="ts">
import { nextTick, ref, watch } from 'vue';
import type { CardReferenceOption } from '../../lib/cardReferences';
import CardWorkspaceIcon from './CardWorkspaceIcon.vue';

const props = defineProps<{
  id: string;
  options: CardReferenceOption[];
  activeIndex: number;
  maxHeight: number;
  busy: boolean;
}>();
defineEmits<{ choose: [option: CardReferenceOption]; highlight: [index: number] }>();
const list = ref<HTMLElement>();
watch(
  () => props.activeIndex,
  async () => {
    await nextTick();
    list.value?.querySelector<HTMLElement>('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' });
  },
);
</script>

<template>
  <div class="reference-picker" :style="{ maxHeight: `${maxHeight}px` }" @mousedown.prevent>
    <div class="reference-heading">引用角色 <span>输入名称筛选</span></div>
    <div :id="id" ref="list" class="reference-options" role="listbox" aria-label="引用角色" :aria-busy="busy">
      <button
        v-for="(option, index) in options"
        :id="`${id}-${index}`"
        :key="option.key"
        type="button"
        role="option"
        tabindex="-1"
        :aria-selected="index === activeIndex"
        :disabled="busy"
        :title="option.name"
        @pointermove="$emit('highlight', index)"
        @click="$emit('choose', option)"
      >
        <CardWorkspaceIcon :name="option.persona ? 'person' : option.attached ? 'book' : 'file'" />
        <span class="reference-name">{{ option.name }}</span>
        <span class="reference-detail">{{ option.detail }}</span>
      </button>
      <p v-if="!options.length" class="reference-empty" role="status">没有匹配的角色</p>
    </div>
    <div class="reference-help">↑↓ 选择 · Tab / Enter 确认 · Esc 关闭</div>
  </div>
</template>

<style scoped>
.reference-picker {
  position: absolute;
  z-index: 15;
  bottom: calc(100% + 8px);
  left: 0;
  right: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  padding: 8px;
  border: 1px solid var(--border-control);
  border-radius: var(--radius-md);
  background: var(--surface-panel);
  box-shadow: var(--shadow-pop);
  color: var(--text-primary);
  font: var(--fs-md) / 1.5 var(--font-ui);
}
.reference-heading,
.reference-help {
  flex: 0 0 auto;
  padding: 4px 8px;
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.reference-heading {
  display: flex;
  justify-content: space-between;
  gap: 8px;
}
.reference-options {
  min-height: 0;
  overflow: auto;
  overscroll-behavior: contain;
}
.reference-options button {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 9px 10px;
  border: 0;
  border-radius: var(--radius-xs);
  background: transparent;
  color: var(--text-primary);
  text-align: left;
  font: var(--fs-md) / 1.5 var(--font-ui);
}
.reference-options button[aria-selected='true'] {
  background: var(--surface-hover);
}
.reference-options svg {
  color: var(--accent-hover);
}
.reference-name {
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.reference-detail {
  flex: 0 0 auto;
  margin-left: auto;
  color: var(--text-muted);
  font-size: var(--fs-xs);
}
.reference-help {
  padding-top: 8px;
}
.reference-empty {
  padding: 16px 8px;
  margin: 0;
  color: var(--text-muted);
}
@media (max-width: 620px) {
  .reference-options button {
    gap: 7px;
    padding: 10px 7px;
  }
  .reference-detail {
    font-size: var(--fs-2xs);
  }
}
</style>
