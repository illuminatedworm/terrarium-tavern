<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { positionPopover } from '../../lib/anchoredPopover';

const props = withDefaults(
  defineProps<{
    original: string;
    busy?: boolean;
    notifyChoice?: boolean | null;
    askNotify?: boolean;
    saveLabel?: string;
  }>(),
  { notifyChoice: null, askNotify: true, saveLabel: '保存修改' },
);
const text = defineModel<string>({ required: true });
const emit = defineEmits<{ cancel: []; save: [notifyAi: boolean] }>();
const root = ref<HTMLElement>();
const textarea = ref<HTMLTextAreaElement>();
const saveButton = ref<HTMLButtonElement>();
const popup = ref<HTMLElement>();
const choice = ref(props.notifyChoice);
let observer: ResizeObserver | undefined;
let appearance: MutationObserver | undefined;
let frame = 0;
let width = 0;
let disposed = false;

function resize() {
  const element = textarea.value;
  if (!element || disposed) return;
  const scroller = element.closest<HTMLElement>('.messages');
  const top = scroller?.scrollTop;
  element.style.height = '0px';
  element.style.height = `${Math.max(element.scrollHeight, parseFloat(getComputedStyle(element).lineHeight) || 1)}px`;
  // Measuring with height=0 briefly shrinks the scroll range. Restore the
  // reader's position after the full height is back, including long pastes.
  if (scroller && top !== undefined) scroller.scrollTop = top;
}
function scheduleResize() {
  cancelAnimationFrame(frame);
  frame = requestAnimationFrame(resize);
}
function closePrompt() {
  popup.value?.hidePopover();
}
function repositionPrompt() {
  if (popup.value?.matches(':popover-open') && saveButton.value)
    positionPopover(popup.value, saveButton.value);
}
async function requestSave() {
  if (props.busy) return;
  if (text.value === props.original) {
    emit('cancel');
    return;
  }
  if (!text.value.trim()) return;
  if (!props.askNotify || choice.value !== null) {
    emit('save', choice.value === true);
    return;
  }
  popup.value?.showPopover();
  await nextTick();
  if (popup.value && saveButton.value) {
    positionPopover(popup.value, saveButton.value);
    popup.value.querySelector<HTMLButtonElement>('button')?.focus({ preventScroll: true });
  }
}
function choose(notifyAi: boolean) {
  if (props.busy) return;
  choice.value = notifyAi;
  closePrompt();
  emit('save', notifyAi);
}
function keydown(event: KeyboardEvent) {
  if (event.isComposing || event.keyCode === 229) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    if (popup.value?.matches(':popover-open')) closePrompt();
    else if (!props.busy) emit('cancel');
  } else if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    void requestSave();
  }
}
function promptEscape() {
  closePrompt();
  saveButton.value?.focus({ preventScroll: true });
}
watch(text, () => {
  closePrompt();
  void nextTick(resize);
});
watch(
  () => props.notifyChoice,
  (value) => {
    choice.value = value;
  },
);
watch(
  () => props.busy,
  (busy) => {
    if (busy) closePrompt();
  },
);
onMounted(async () => {
  await nextTick();
  if (disposed) return;
  resize();
  textarea.value?.focus({ preventScroll: true });
  observer = new ResizeObserver((entries) => {
    const measured = entries[0]?.contentRect.width || 0;
    if (measured !== width) {
      width = measured;
      scheduleResize();
      closePrompt();
    }
  });
  if (root.value) observer.observe(root.value);
  // Reading preferences are CSS variables on ancestors, not editor props.
  appearance = new MutationObserver(scheduleResize);
  for (let node = root.value?.parentElement; node; node = node.parentElement)
    appearance.observe(node, { attributes: true, attributeFilter: ['style', 'class'] });
  void document.fonts.ready.then(() => {
    if (!disposed) scheduleResize();
  });
});
window.addEventListener('resize', repositionPrompt);
window.addEventListener('scroll', repositionPrompt, true);
onBeforeUnmount(() => {
  disposed = true;
  cancelAnimationFrame(frame);
  observer?.disconnect();
  appearance?.disconnect();
  closePrompt();
  window.removeEventListener('resize', repositionPrompt);
  window.removeEventListener('scroll', repositionPrompt, true);
});
</script>

<template>
  <div ref="root" class="inline-message-editor" :aria-busy="busy">
    <textarea
      ref="textarea"
      v-model="text"
      rows="1"
      aria-label="编辑消息原文"
      :readonly="busy"
      @input="resize"
      @keydown="keydown"
    />
    <div class="inline-edit-actions">
      <span>{{
        choice === null ? 'Ctrl + Enter 保存 · Esc 取消' : choice ? '保存后提醒 AI' : '保存后不提醒 AI'
      }}</span>
      <button type="button" :disabled="busy" @click="emit('cancel')">取消</button>
      <button
        ref="saveButton"
        type="button"
        class="primary"
        :disabled="busy || !text.trim()"
        @click="requestSave"
      >
        {{ busy ? '保存中…' : saveLabel }}
      </button>
    </div>
  </div>
  <div
    ref="popup"
    popover="auto"
    class="message-edit-popover edit-save-question"
    role="dialog"
    aria-label="选择是否提醒 AI"
    @keydown.esc.prevent.stop="promptEscape"
    @click.stop
  >
    <p>这次修改要提醒 AI 吗？</p>
    <div>
      <button type="button" :disabled="busy" @click="choose(true)">提醒 AI</button>
      <button type="button" :disabled="busy" @click="choose(false)">不提醒 AI</button>
    </div>
  </div>
</template>
