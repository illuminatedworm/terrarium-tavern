<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue';
const props = defineProps<{ disabled?: boolean; title: string }>();
const emit = defineEmits<{ confirm: [] }>();
const trigger = ref<HTMLButtonElement>();
const popup = ref<HTMLElement>();
const cancel = ref<HTMLButtonElement>();
function close() {
  popup.value?.hidePopover();
}
async function open() {
  if (props.disabled) return;
  popup.value?.showPopover();
  await nextTick();
  const anchor = trigger.value!.getBoundingClientRect();
  const box = popup.value!.getBoundingClientRect();
  popup.value!.style.left =
    Math.max(8, Math.min(anchor.right - box.width, innerWidth - box.width - 8)) + 'px';
  popup.value!.style.top =
    Math.max(
      8,
      anchor.bottom + box.height + 8 < innerHeight ? anchor.bottom + 6 : anchor.top - box.height - 6,
    ) + 'px';
  cancel.value?.focus();
}
function confirmRemoval() {
  if (props.disabled) return;
  close();
  emit('confirm');
}
watch(() => props.disabled, close);
window.addEventListener('resize', close);
window.addEventListener('scroll', close, true);
onBeforeUnmount(() => {
  window.removeEventListener('resize', close);
  window.removeEventListener('scroll', close, true);
});
</script>

<template>
  <button
    ref="trigger"
    class="lore-remove"
    :disabled="disabled"
    :aria-label="`移除注入：${title}`"
    aria-haspopup="dialog"
    @click.stop.prevent="open"
  >
    移除
  </button>
  <div
    ref="popup"
    popover="auto"
    class="lore-popconfirm"
    role="dialog"
    aria-label="确认移除世界书注入"
    @click.stop
    @keydown.esc.stop="close"
  >
    <strong>移除这条世界书注入？</strong>
    <p>移除后不再发送给 AI。原有注入限制会保留，随所在剧情退出窗口后才开始冷却。</p>
    <div class="lore-confirm-actions">
      <button ref="cancel" @click="close">取消</button>
      <button :disabled="disabled" @click="confirmRemoval">确认移除</button>
    </div>
  </div>
</template>

<style scoped>
.lore-remove {
  margin-left: auto;
  flex-shrink: 0;
  font-size: var(--fs-sm);
}
.lore-popconfirm {
  position: fixed;
  inset: auto;
  margin: 0;
  width: min(300px, calc(100vw - 16px));
  max-height: calc(100dvh - 16px);
  overflow: auto;
  padding: 14px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--surface-panel);
  color: var(--text-primary);
  box-shadow: var(--shadow-pop);
  font: var(--fs-md) / 1.7 var(--font-ui);
  white-space: normal;
}
.lore-popconfirm p {
  margin: 8px 0 12px;
  color: var(--text-secondary);
}
.lore-confirm-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
</style>
