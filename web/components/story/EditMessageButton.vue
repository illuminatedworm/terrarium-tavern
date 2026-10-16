<script setup lang="ts">
import Icon from '../common/Icon.vue';
import { nextTick, onBeforeUnmount, ref, useId, watch } from 'vue';
import { positionPopover } from '../../lib/anchoredPopover';

const props = defineProps<{ disabled?: boolean }>();
const emit = defineEmits<{ edit: [notifyAi: boolean | null] }>();
const trigger = ref<HTMLButtonElement>();
const popup = ref<HTMLElement>();
const active = ref<boolean | null>(null);
const menuOpen = ref(false);
const popupId = useId();
let timer: ReturnType<typeof setTimeout> | undefined;
let pointerId: number | null = null;
let held = false;
let suppressClick = false;
let startX = 0,
  startY = 0;

function clearPress() {
  clearTimeout(timer);
  timer = undefined;
  const captured = pointerId;
  pointerId = null;
  if (captured !== null && trigger.value?.hasPointerCapture(captured))
    trigger.value.releasePointerCapture(captured);
}
function close() {
  clearPress();
  popup.value?.hidePopover();
  menuOpen.value = false;
  active.value = null;
}
async function open(keyboard = false) {
  if (props.disabled || !trigger.value || !popup.value) return;
  popup.value.showPopover();
  menuOpen.value = true;
  await nextTick();
  if (!popup.value.matches(':popover-open')) return;
  positionPopover(popup.value, trigger.value);
  if (keyboard) popup.value.querySelector<HTMLButtonElement>('button')?.focus();
}
function choose(notifyAi: boolean) {
  if (props.disabled) return;
  close();
  emit('edit', notifyAi);
}
function down(event: PointerEvent) {
  if (props.disabled || !event.isPrimary || event.button !== 0) return;
  close();
  held = false;
  suppressClick = false;
  pointerId = event.pointerId;
  startX = event.clientX;
  startY = event.clientY;
  trigger.value?.setPointerCapture(event.pointerId);
  timer = setTimeout(() => {
    held = true;
    suppressClick = true;
    void open();
  }, 300);
}
function optionAt(event: PointerEvent) {
  const hit = document.elementFromPoint(event.clientX, event.clientY);
  const option = hit?.closest<HTMLElement>('[data-edit-notify]');
  return option && popup.value?.contains(option) ? option.dataset.editNotify === 'true' : null;
}
function move(event: PointerEvent) {
  if (pointerId !== event.pointerId) return;
  if (held) active.value = optionAt(event);
  else if (Math.hypot(event.clientX - startX, event.clientY - startY) > 8) {
    clearTimeout(timer);
    timer = undefined;
  }
}
function up(event: PointerEvent) {
  if (pointerId !== event.pointerId) return;
  const hit = document.elementFromPoint(event.clientX, event.clientY);
  const option = optionAt(event);
  const onTrigger = !!hit && !!trigger.value?.contains(hit);
  clearPress();
  if (held) {
    suppressClick = true;
    if (option !== null) choose(option);
    else if (!onTrigger) close();
  } else if (!onTrigger) {
    suppressClick = true;
    close();
  }
}
function click(event: MouseEvent) {
  if (suppressClick && event.detail !== 0) {
    suppressClick = false;
    return;
  }
  if (!props.disabled) emit('edit', null);
}
function escape() {
  close();
  trigger.value?.focus({ preventScroll: true });
}
function outside(event: PointerEvent) {
  const target = event.target;
  if (target instanceof Node && !popup.value?.contains(target) && !trigger.value?.contains(target)) close();
}
function focusOutside(event: FocusEvent) {
  if (!menuOpen.value || pointerId !== null) return;
  const target = event.target;
  if (target instanceof Node && !popup.value?.contains(target) && !trigger.value?.contains(target)) close();
}
function menuKeys(event: KeyboardEvent) {
  if (!['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  const buttons = Array.from(popup.value?.querySelectorAll<HTMLButtonElement>('button') || []);
  const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
  buttons[
    event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (index + 1) % buttons.length
  ]?.focus();
}
watch(
  () => props.disabled,
  (disabled) => {
    if (disabled) close();
  },
);
window.addEventListener('resize', close);
window.addEventListener('scroll', close, true);
window.addEventListener('blur', close);
document.addEventListener('pointerdown', outside, true);
document.addEventListener('focusin', focusOutside);
onBeforeUnmount(() => {
  close();
  window.removeEventListener('resize', close);
  window.removeEventListener('scroll', close, true);
  window.removeEventListener('blur', close);
  document.removeEventListener('pointerdown', outside, true);
  document.removeEventListener('focusin', focusOutside);
});
</script>

<template>
  <button
    ref="trigger"
    type="button"
    class="icon-action message-edit-trigger"
    :disabled="disabled"
    aria-label="编辑"
    title="编辑本条消息；长按选择是否提醒 AI"
    aria-haspopup="menu"
    :aria-expanded="menuOpen"
    :aria-controls="popupId"
    @pointerdown="down"
    @pointermove="move"
    @pointerup="up"
    @pointercancel="close"
    @click="click"
    @contextmenu.prevent
    @keydown.shift.f10.prevent="open(true)"
    @keydown.esc.prevent="escape"
  >
    <Icon name="pencil" :size="16" />
  </button>
  <div
    ref="popup"
    :id="popupId"
    popover="manual"
    class="message-edit-popover edit-choice-menu"
    role="menu"
    aria-label="选择修改后的 AI 提醒方式"
    @keydown.esc.prevent.stop="escape"
    @keydown="menuKeys"
    @click.stop
  >
    <button
      type="button"
      role="menuitem"
      data-edit-notify="true"
      :class="{ selected: active === true }"
      @click="choose(true)"
    >
      修改后，提醒 AI
    </button>
    <button
      type="button"
      role="menuitem"
      data-edit-notify="false"
      :class="{ selected: active === false }"
      @click="choose(false)"
    >
      不提醒 AI
    </button>
  </div>
</template>

<style scoped>
.message-edit-trigger {
  touch-action: none;
  user-select: none;
}
</style>
