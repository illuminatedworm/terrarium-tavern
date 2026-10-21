<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';

const props = defineProps<{ original: string; busy?: boolean }>();
const text = defineModel<string>({ required: true });
const emit = defineEmits<{ cancel: []; save: [] }>();
const root = ref<HTMLElement>();
const textarea = ref<HTMLTextAreaElement>();
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
function requestSave() {
  if (props.busy || !text.value.trim()) return;
  if (text.value === props.original) emit('cancel');
  else emit('save');
}
function keydown(event: KeyboardEvent) {
  if (event.isComposing || event.keyCode === 229) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    if (!props.busy) emit('cancel');
  } else if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    requestSave();
  }
}
watch(text, () => void nextTick(resize));
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
onBeforeUnmount(() => {
  disposed = true;
  cancelAnimationFrame(frame);
  observer?.disconnect();
  appearance?.disconnect();
});
</script>

<template>
  <div ref="root" class="inline-message-editor" :aria-busy="busy">
    <textarea
      ref="textarea"
      v-model="text"
      rows="1"
      aria-label="修改用户输入"
      :readonly="busy"
      @input="resize"
      @keydown="keydown"
    />
    <div class="inline-edit-actions">
      <span>Ctrl + Enter 修改并重试 · Esc 取消</span>
      <button type="button" :disabled="busy" @click="emit('cancel')">取消</button>
      <button type="button" class="primary" :disabled="busy || !text.trim()" @click="requestSave">
        {{ busy ? '正在发起重试…' : '修改并重试' }}
      </button>
    </div>
  </div>
</template>
