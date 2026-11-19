<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue';
import { positionPopover } from '../../lib/anchoredPopover';
import { cardName, isWorldbook, type EditorCard } from '../../lib/cardAuthor';
import Icon from '../common/Icon.vue';

const props = defineProps<{
  cards: EditorCard[];
  modelValue: string[];
  skill?: string;
  continuing: boolean;
  disabled: boolean;
}>();
const emit = defineEmits<{ 'update:modelValue': [refs: string[]]; removeSkill: [] }>();
const selection = computed({
  get: () => props.modelValue,
  set: (refs: string[]) => emit('update:modelValue', refs),
});
const labels = computed(() =>
  selection.value.map((ref) => {
    const card = props.cards.find((c) => c.ref === ref);
    return { ref, name: card ? cardName(card) : ref };
  }),
);
const fullLabel = computed(() => labels.value.map((c) => `${c.name} (${c.ref})`).join('、') || '未指定');
const first = computed(() => labels.value[0]);
const namesCollide = computed(
  () => first.value && props.cards.filter((c) => cardName(c) === first.value.name).length > 1,
);
const menuId = useId();
const trigger = ref<HTMLButtonElement>();
const menu = ref<HTMLElement>();
const open = ref(false);

function position() {
  if (!menu.value?.matches(':popover-open') || !trigger.value) return;
  const box = trigger.value.getBoundingClientRect();
  menu.value.style.maxHeight = `${Math.min(300, Math.max(box.top, innerHeight - box.bottom) - 16)}px`;
  positionPopover(menu.value, trigger.value, 'above');
}
function toggled(event: Event) {
  open.value = (event as ToggleEvent).newState === 'open';
  if (open.value) {
    position();
    const firstInput =
      menu.value?.querySelector<HTMLInputElement>('input:checked:not(:disabled)') ??
      menu.value?.querySelector<HTMLInputElement>('input:not(:disabled)');
    firstInput?.focus({ preventScroll: true });
  }
}
function focusLeft(event: FocusEvent) {
  const next = event.relatedTarget;
  if (next instanceof Node && next !== trigger.value && !menu.value?.contains(next))
    menu.value?.hidePopover();
}
watch(open, (visible, _, cleanup) => {
  if (!visible || !trigger.value) return;
  const observer = new ResizeObserver(position);
  observer.observe(trigger.value);
  window.addEventListener('resize', position);
  cleanup(() => {
    observer.disconnect();
    window.removeEventListener('resize', position);
  });
});
watch(
  () => props.disabled || props.continuing,
  (locked) => {
    if (locked) menu.value?.hidePopover();
  },
);
</script>

<template>
  <div class="cw-task-scope" role="group" aria-label="本轮任务">
    <span v-if="continuing" class="scope-control scope-resume" :title="fullLabel">
      <Icon name="clock" :size="14" />
      <span class="scope-caption">续接原任务</span>
      <span v-if="first" class="scope-value">{{ fullLabel }}</span>
    </span>
    <template v-else>
      <button
        ref="trigger"
        class="scope-control scope-trigger"
        type="button"
        :popovertarget="menuId"
        aria-haspopup="dialog"
        :aria-expanded="open"
        :aria-controls="menuId"
        :aria-label="`选择本轮目标，${fullLabel}`"
        :title="fullLabel"
        :disabled="disabled"
      >
        <Icon name="target" :size="14" />
        <span class="scope-caption">本轮目标</span>
        <span class="scope-value">{{ first?.name || '未指定' }}</span>
        <small v-if="namesCollide" class="scope-id">{{ first.ref }}</small>
        <small v-if="labels.length > 1" class="scope-count">+{{ labels.length - 1 }}</small>
        <Icon name="chevron-down" :size="12" class="scope-chevron" />
      </button>
      <div
        ref="menu"
        :id="menuId"
        popover="auto"
        class="scope-menu"
        role="dialog"
        aria-label="选择本轮编辑目标"
        @toggle="toggled"
        @focusout="focusLeft"
      >
        <div class="scope-heading"><strong>本轮目标</strong><span>可多选</span></div>
        <label v-for="card in cards" :key="card.ref" class="scope-option">
          <input
            v-model="selection"
            type="checkbox"
            :value="card.ref"
            :disabled="disabled || (!!skill && isWorldbook(card))"
          />
          <span>{{ cardName(card) }}</span>
          <small>{{ isWorldbook(card) ? '世界书' : '角色卡' }} · {{ card.ref }}</small>
        </label>
      </div>
    </template>
    <span v-if="skill" class="scope-control scope-skill">
      <Icon name="book" :size="14" />
      <span class="scope-skill-name" :title="skill">{{ skill }}</span>
      <button
        v-if="!continuing"
        class="scope-remove"
        type="button"
        :disabled="disabled"
        :aria-label="`移除流程 ${skill}`"
        title="移除流程"
        @click="emit('removeSkill')"
      >
        <Icon name="close" :size="12" />
      </button>
    </span>
  </div>
</template>

<style scoped>
.cw-task-scope {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  min-width: 0;
  max-width: 100%;
}
.scope-control {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: var(--cw-context-height);
  min-width: 0;
  max-width: 100%;
  margin: 0;
  padding: 0 8px;
  border: 1px solid var(--border-control);
  border-radius: var(--radius-xs);
  background: var(--surface-hover);
  color: var(--text-primary);
  font: var(--fs-sm) / 1.3 var(--font-ui);
  white-space: nowrap;
}
.scope-control > svg,
.scope-caption,
.scope-id,
.scope-count {
  flex: none;
}
.scope-trigger > svg:first-child,
.scope-resume > svg {
  color: var(--accent-hover);
}
.scope-caption,
.scope-chevron {
  color: var(--text-muted);
}
.scope-value,
.scope-skill-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}
.scope-value {
  max-width: 12em;
  padding-left: 7px;
  border-left: 1px solid var(--border-control);
  font-weight: 500;
}
.scope-id,
.scope-count {
  color: var(--text-secondary);
  font: var(--fs-xs) / 1 var(--font-mono);
}
.scope-trigger:hover:not(:disabled),
.scope-trigger[aria-expanded='true'] {
  background: var(--surface-active);
  border-color: var(--glass-edge-strong);
}
.scope-trigger[aria-expanded='true'] .scope-chevron {
  transform: rotate(180deg);
}
.scope-skill {
  gap: 5px;
  border-color: rgb(var(--accent-rgb) / 0.24);
  background: var(--accent-soft);
  color: var(--accent-hover);
}
.scope-skill:has(.scope-remove) {
  padding-right: 2px;
}
.scope-remove {
  display: grid;
  place-items: center;
  flex: none;
  width: calc(var(--cw-context-height) - 6px);
  height: calc(var(--cw-context-height) - 6px);
  padding: 0;
  border: 0;
  border-radius: var(--radius-2xs);
  background: transparent;
  color: inherit;
}
.scope-remove:hover:not(:disabled) {
  background: var(--hover-wash);
  color: var(--text-strong);
}
.scope-menu {
  position: fixed;
  inset: auto;
  width: min(272px, calc(100vw - 16px));
  margin: 0;
  padding: 5px;
  overflow: auto;
  overscroll-behavior: contain;
  border: 1px solid var(--glass-edge-strong);
  border-radius: var(--radius-md);
  background: var(--glass-sheen), var(--glass-float);
  -webkit-backdrop-filter: var(--glass-blur-strong);
  backdrop-filter: var(--glass-blur-strong);
  box-shadow: var(--glass-highlight), var(--shadow-pop);
  color: var(--text-primary);
  font: var(--fs-md) / 1.4 var(--font-ui);
}
.scope-heading,
.scope-option {
  display: flex;
  flex-direction: row;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin: 0;
  padding: 6px 9px;
}
.scope-heading {
  margin-bottom: 3px;
  color: var(--text-secondary);
  font-size: var(--fs-xs);
}
.scope-heading span,
.scope-option small {
  flex: none;
  color: var(--text-muted);
  font-size: var(--fs-xs);
}
.scope-option {
  min-height: var(--control-h);
  border-radius: var(--radius-xs);
  cursor: pointer;
}
.scope-option > input {
  flex: none;
  margin: 0;
}
.scope-option > span {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
}
.scope-option:has(:checked) {
  color: var(--accent-hover);
  background: var(--accent-soft);
}
.scope-option:hover,
.scope-option:focus-within {
  background: var(--hover-wash);
}
.scope-option:has(:checked):is(:hover, :focus-within) {
  background: rgb(var(--accent-rgb) / 0.2);
}
.scope-option:has(:disabled) {
  opacity: 0.5;
  cursor: not-allowed;
}
@media (pointer: coarse) {
  .scope-option {
    min-height: 44px;
  }
}
@container composer (max-width: 420px) {
  .cw-task-scope {
    flex-wrap: nowrap;
  }
  .scope-control {
    gap: 4px;
    padding-inline: 6px;
  }
  .scope-value {
    padding-left: 5px;
  }
  .scope-skill:has(.scope-remove) {
    padding-right: 2px;
  }
}
</style>
