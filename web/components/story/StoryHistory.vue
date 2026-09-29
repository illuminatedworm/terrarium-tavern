<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, shallowRef, watch } from 'vue';
import { HistoryHeights, needsVirtualHistory, type HistoryMessage } from '../../lib/storyHistory';
import { storyHtml } from '../../lib/storyHtml';

const props = defineProps<{
  messages: HistoryMessage[];
  scroller?: HTMLElement;
  editingId?: string;
  following?: boolean;
  retainInteractive?: boolean;
}>();
const emit = defineEmits<{ visible: [messages: HistoryMessage[]]; reading: [id: string] }>();
const root = ref<HTMLElement>();
const virtual = ref(false);
const viewport = ref({ top: 0, height: 800 });
const revision = ref(0);
const heights = new HistoryHeights();
const measured = new Map<string, number>();
const nodes = new Map<string, HTMLElement>();
const retained = shallowRef(new Set<string>());
const interactiveChecks = new Map<string, { text: string; interactive: boolean }>();
const indices = computed(() => new Map(props.messages.map((message, index) => [message.id, index])));
const total = computed(() => {
  void revision.value;
  return heights.offset(props.messages.length);
});
const range = computed(() => {
  void revision.value;
  if (!virtual.value) return { first: 0, last: props.messages.length - 1 };
  return {
    first: heights.indexAt(Math.max(0, viewport.value.top - viewport.value.height)),
    last: heights.indexAt(viewport.value.top + viewport.value.height * 2),
  };
});
const visible = computed(() => props.messages.slice(range.value.first, range.value.last + 1));
const rendered = computed(() => {
  if (!virtual.value) return props.messages;
  const selected = new Set(visible.value.map((message) => message.id));
  for (const id of retained.value) if (indices.value.has(id)) selected.add(id);
  if (props.editingId && indices.value.has(props.editingId)) selected.add(props.editingId);
  return [...selected]
    .sort((left, right) => indices.value.get(left)! - indices.value.get(right)!)
    .map((id) => props.messages[indices.value.get(id)!]);
});
let observer: ResizeObserver | undefined;
let scrollFrame = 0;
let disposed = false;
let jumpVersion = 0;

function origin() {
  if (!root.value || !props.scroller) return 0;
  return (
    root.value.getBoundingClientRect().top -
    props.scroller.getBoundingClientRect().top +
    props.scroller.scrollTop
  );
}
function updateViewport() {
  const scroller = props.scroller;
  if (!scroller || !root.value) return;
  const top = scroller.scrollTop - origin();
  viewport.value = { top, height: scroller.clientHeight };
  if (virtual.value) {
    const message = props.messages[heights.indexAt(Math.max(0, top + 24))];
    if (message) emit('reading', message.id);
  } else {
    const edge = scroller.getBoundingClientRect().top + 24;
    const message = props.messages.find(
      (item) => (nodes.get(item.id)?.getBoundingClientRect().bottom || 0) > edge,
    );
    if (message) emit('reading', message.id);
  }
}
function scheduleViewport() {
  cancelAnimationFrame(scrollFrame);
  scrollFrame = requestAnimationFrame(updateViewport);
}
window.addEventListener('resize', scheduleViewport);
function rowRef(id: string, node: unknown) {
  const previous = nodes.get(id);
  if (previous === node) return;
  if (previous) observer?.unobserve(previous);
  if (node instanceof HTMLElement) {
    nodes.set(id, node);
    observer?.observe(node);
  } else nodes.delete(id);
}
async function measure(entries: ResizeObserverEntry[]) {
  const scroller = props.scroller;
  if (!scroller || disposed) return;
  const top = scroller.scrollTop - origin();
  const anchor = heights.indexAt(Math.max(0, top));
  const previousOffset = heights.offset(anchor);
  const following = props.following;
  let changed = false;
  for (const entry of entries) {
    const node = entry.target as HTMLElement;
    const id = node.dataset.historyId;
    const index = id ? indices.value.get(id) : undefined;
    if (id && index !== undefined && !node.classList.contains('history-retained')) {
      const height = node.getBoundingClientRect().height;
      if (height > 0) {
        measured.set(id, height);
        changed = heights.set(index, height) || changed;
      }
    }
  }
  if (changed) {
    revision.value++;
    const correction = heights.offset(anchor) - previousOffset;
    await nextTick();
    if (disposed) return;
    if (following) scroller.scrollTop = scroller.scrollHeight;
    else if (correction) scroller.scrollTop += correction;
  }
  updateViewport();
}
function rowStyle(id: string) {
  if (!virtual.value) return undefined;
  void revision.value;
  return {
    position: 'absolute' as const,
    top: `${heights.offset(indices.value.get(id)!)}px`,
    left: '0',
    right: '0',
    containIntrinsicSize: `auto ${measured.get(id) || 280}px`,
  };
}
async function jumpTo(id: string) {
  const index = indices.value.get(id);
  const scroller = props.scroller;
  if (index === undefined || !scroller) return;
  const version = ++jumpVersion;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (disposed || version !== jumpVersion) return;
    if (virtual.value) scroller.scrollTop = origin() + heights.offset(index);
    else {
      const node = nodes.get(id);
      if (node) scroller.scrollTop += node.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
    }
    updateViewport();
    await nextTick();
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
  }
}
watch(
  () => [props.messages, props.messages.length],
  async () => {
    const previousTop = props.scroller?.scrollTop || 0;
    let readingId: string | undefined;
    let readingOffset = 0;
    if (!virtual.value && needsVirtualHistory(props.messages)) {
      const edge = props.scroller?.getBoundingClientRect().top || 0;
      for (const [id, node] of nodes) {
        measured.set(id, node.getBoundingClientRect().height);
        if (!readingId && node.getBoundingClientRect().bottom > edge) {
          readingId = id;
          readingOffset = node.getBoundingClientRect().top - edge;
        }
      }
      virtual.value = true;
    }
    heights.reset(
      props.messages.map(
        (message) =>
          measured.get(message.id) ||
          Math.min(2000, 100 + Math.ceil((message.display || message.raw || '').length / 45) * 28),
      ),
    );
    revision.value++;
    await nextTick();
    if (disposed) return;
    if (virtual.value && !observer) {
      observer = new ResizeObserver((entries) => void measure(entries));
      if (props.scroller) observer.observe(props.scroller);
      for (const node of nodes.values()) observer.observe(node);
    }
    if (readingId && props.scroller && !props.following) {
      props.scroller.scrollTop = origin() + heights.offset(indices.value.get(readingId) || 0) - readingOffset;
    } else if (!props.following && props.scroller) props.scroller.scrollTop = previousTop;
    updateViewport();
  },
  { immediate: true },
);
watch(
  () => [visible.value, props.retainInteractive] as const,
  ([messages]) => {
    if (props.retainInteractive) {
      const next = new Set(retained.value);
      for (const message of messages) {
        if (next.has(message.id)) continue;
        const text = message.display || message.raw || '';
        let checked = interactiveChecks.get(message.id);
        if (!checked || checked.text !== text) {
          checked = { text, interactive: !!storyHtml(text) };
          interactiveChecks.set(message.id, checked);
        }
        if (checked.interactive) next.add(message.id);
      }
      if (next.size !== retained.value.size) retained.value = next;
    }
    emit('visible', messages);
  },
  { immediate: true },
);
watch(
  () => props.scroller,
  (scroller, previous) => {
    previous?.removeEventListener('scroll', scheduleViewport);
    scroller?.addEventListener('scroll', scheduleViewport, { passive: true });
    if (scroller) observer?.observe(scroller);
    scheduleViewport();
  },
  { immediate: true },
);
onBeforeUnmount(() => {
  disposed = true;
  jumpVersion++;
  observer?.disconnect();
  cancelAnimationFrame(scrollFrame);
  props.scroller?.removeEventListener('scroll', scheduleViewport);
  window.removeEventListener('resize', scheduleViewport);
});
defineExpose({ jumpTo, updateViewport });
</script>

<template>
  <div
    ref="root"
    class="story-history"
    :data-virtual="virtual"
    :style="virtual ? { height: total + 'px' } : undefined"
  >
    <div
      v-for="message in rendered"
      :key="message.id"
      :ref="(node) => rowRef(message.id, node)"
      :data-history-id="message.id"
      class="history-row"
      :style="rowStyle(message.id)"
      :class="{ 'history-retained': virtual && retained.has(message.id) && !visible.includes(message) }"
    >
      <slot :message="message" />
    </div>
  </div>
</template>

<style scoped>
.story-history {
  position: relative;
}
.history-row {
  display: flow-root;
}
.history-retained {
  content-visibility: hidden;
}
</style>
