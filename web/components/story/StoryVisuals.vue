<script setup lang="ts">
import { defineAsyncComponent, h, nextTick, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue';
import { loadVisuals, observeVisuals, type VisualSpec } from '../../lib/visuals';
import Icon from '../common/Icon.vue';
const props = defineProps<{ sessionId: string; messageId: string }>();
const emit = defineEmits<{ compose: [text: string] }>();
const root = ref<HTMLElement>();
const dialog = ref<HTMLDialogElement>();
const dialogContent = ref<HTMLElement>();
const dialogReady = ref(false);
const expanded = ref<number | null>(null);
const specs = shallowRef<VisualSpec[]>([]);
const loading = ref(false);
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
  } else void load();
}
let generation = 0;
let stopObserving: (() => void) | undefined;
let previousFocus: HTMLElement | null = null;
async function load() {
  const current = ++generation;
  loading.value = true;
  error.value = '';
  try {
    const result = await loadVisuals(props.sessionId, props.messageId);
    if (current === generation) specs.value = result;
  } catch (e) {
    if (current === generation) error.value = e instanceof Error ? e.message : String(e);
  } finally {
    if (current === generation) loading.value = false;
  }
}
// The parent keys this component by session and message, so a new identity
// always remounts it; there is no in-place message switch to handle here.
onMounted(() => {
  stopObserving = observeVisuals(root.value!, () => void load());
});
onBeforeUnmount(() => {
  generation++;
  stopObserving?.();
  dialog.value?.close();
});
async function expand(index: number) {
  previousFocus = document.activeElement as HTMLElement;
  dialogReady.value = true;
  await nextTick();
  expanded.value = index;
  await nextTick();
  dialog.value!.showModal();
}
function closed() {
  expanded.value = null;
  previousFocus?.focus();
}
function compose(text: string) {
  emit('compose', text);
  dialog.value?.close();
}
</script>
<template>
  <div ref="root" class="story-visuals">
    <p v-if="error" class="error" role="alert">
      图形加载失败：{{ error }} <button :disabled="loading" @click="retry">重试</button>
    </p>
    <section v-for="(spec, index) in specs" :key="index" class="story-visual" aria-label="交互图形">
      <header class="visual-header">
        <span>本轮图形 <small>保留生成时的内容</small></span>
        <button class="quiet" @click="expand(index)"><Icon name="external" :size="14" />放大查看</button>
      </header>
      <Teleport :to="dialogContent || 'body'" :disabled="expanded !== index">
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
