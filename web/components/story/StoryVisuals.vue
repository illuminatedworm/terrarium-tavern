<script setup lang="ts">
import { defineAsyncComponent, h, nextTick, onBeforeUnmount, ref } from 'vue';
import type { VisualSpec } from '../../lib/visuals';
import Icon from '../common/Icon.vue';
defineProps<{ spec: VisualSpec; preview?: boolean }>();
const emit = defineEmits<{ compose: [text: string] }>();
const dialog = ref<HTMLDialogElement>();
const dialogContent = ref<HTMLElement>();
const dialogReady = ref(false);
const expanded = ref(false);
const error = ref('');
// onError's fail() is deliberately not kept: rejecting on unmount would report a
// loader error to the app handler after the user has already left the message.
let failedLoad: { retry: () => void } | undefined;
const VisualInterface = defineAsyncComponent({
  loader: () => import('./VisualInterface.vue'),
  loadingComponent: { render: () => h('p', { class: 'hint', role: 'status' }, '正在加载图形…') },
  onError(cause, retry) {
    error.value = cause.message;
    failedLoad = { retry };
  },
});
function retry() {
  if (failedLoad) {
    const action = failedLoad.retry;
    failedLoad = undefined;
    error.value = '';
    action();
  }
}
let previousFocus: HTMLElement | null = null;
onBeforeUnmount(() => {
  dialog.value?.close();
});
async function expand() {
  previousFocus = document.activeElement as HTMLElement;
  dialogReady.value = true;
  await nextTick();
  expanded.value = true;
  await nextTick();
  dialog.value!.showModal();
}
function closed() {
  expanded.value = false;
  previousFocus?.focus();
}
function compose(text: string) {
  emit('compose', text);
  dialog.value?.close();
}
</script>
<template>
  <div class="story-visuals">
    <p v-if="error" class="error" role="alert">
      图形加载失败：{{ error }} <button @click="retry">重试</button>
    </p>
    <section class="story-visual" aria-label="交互图形">
      <header class="visual-header">
        <span
          >本轮图形 <small>{{ preview ? '预览 · 尚未提交' : '保留生成时的内容' }}</small></span
        >
        <button class="quiet" @click="expand"><Icon name="external" :size="14" />放大查看</button>
      </header>
      <Teleport :to="dialogContent || 'body'" :disabled="!expanded">
        <VisualInterface :spec="spec" @compose="compose" />
      </Teleport>
    </section>
    <Teleport v-if="dialogReady" to="body">
      <dialog ref="dialog" class="visual-dialog" aria-label="放大查看图形" @close="closed">
        <header class="visual-header">
          <h2>交互图形</h2>
          <button aria-label="关闭图形" title="关闭" @click="dialog?.close()">
            <Icon name="close" :size="16" />
          </button>
        </header>
        <div ref="dialogContent" class="visual-dialog-content"></div>
      </dialog>
    </Teleport>
  </div>
</template>
