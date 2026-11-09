<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';

const props = defineProps<{ original: string; originalOoc?: string; busy?: boolean; opening?: boolean }>();
const text = defineModel<string>({ required: true });
/** Bound only for user inputs, which may carry out-of-character text. */
const ooc = defineModel<string>('ooc');
const emit = defineEmits<{ cancel: []; save: [] }>();
const root = ref<HTMLElement>();
const textarea = ref<HTMLTextAreaElement>();
let observer: ResizeObserver | undefined;
let appearance: MutationObserver | undefined;
let frame = 0;
let width = 0;
let disposed = false;

function resize() {
  if (!root.value || disposed) return;
  const scroller = root.value.closest<HTMLElement>('.messages');
  const top = scroller?.scrollTop;
  for (const element of Array.from(root.value.querySelectorAll('textarea'))) {
    element.style.height = '0px';
    element.style.height = `${Math.max(element.scrollHeight, parseFloat(getComputedStyle(element).lineHeight) || 1)}px`;
  }
  // Measuring with height=0 briefly shrinks the scroll range. Restore the
  // reader's position after the full height is back, including long pastes.
  if (scroller && top !== undefined) scroller.scrollTop = top;
}
function scheduleResize() {
  cancelAnimationFrame(frame);
  frame = requestAnimationFrame(resize);
}
const empty = computed(() => !props.opening && !text.value.trim() && !ooc.value?.trim());
function requestSave() {
  if (props.busy || empty.value) return;
  if (text.value === props.original && (ooc.value ?? '') === (props.originalOoc ?? '')) emit('cancel');
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
watch([text, ooc], () => void nextTick(resize));
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
      :aria-label="opening ? '修改开场白' : '修改用户输入'"
      :readonly="busy"
      @input="resize"
      @keydown="keydown"
    />
    <label v-if="ooc !== undefined" class="inline-edit-ooc">
      <small>场外</small>
      <textarea
        v-model="ooc"
        rows="1"
        aria-label="修改场外发言"
        placeholder="场外发言（可选）"
        :readonly="busy"
        @input="resize"
        @keydown="keydown"
      />
    </label>
    <div class="inline-edit-actions">
      <span v-if="opening"
        >仅修改当前对话；保存会重新初始化开场变量和脚本状态。Ctrl + Enter 保存 · Esc 取消</span
      >
      <span v-else>Ctrl + Enter 修改并重试 · Esc 取消</span>
      <button type="button" :disabled="busy" @click="emit('cancel')">取消</button>
      <button type="button" class="primary" :disabled="busy || empty" @click="requestSave">
        {{ opening ? (busy ? '正在保存…' : '保存开场白') : busy ? '正在发起重试…' : '修改并重试' }}
      </button>
    </div>
  </div>
</template>
