<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, shallowRef, watch } from 'vue';
import { HistoryHeights, needsVirtualHistory, type HistoryMessage } from '../../lib/storyHistory';

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
let pinned: { id: string; edge: 'start' | 'end'; offset: number } | undefined;
let lastBoundary: { id: string; edge: 'start' | 'end'; top: number } | undefined;
function releaseAnchor() {
  pinned = undefined;
  lastBoundary = undefined;
  jumpVersion++;
}
function readingBounds(scroller: HTMLElement) {
  const { top, bottom } = scroller.getBoundingClientRect();
  // The scroll padding reserves the floating composer and its reading gap.
  // End jumps must use that same visible boundary instead of the covered edge.
  return { top, bottom: bottom - (parseFloat(getComputedStyle(scroller).paddingBottom) || 0) };
}
function alignPinned() {
  const scroller = props.scroller;
  if (!pinned || !scroller) return;
  const row = nodes.get(pinned.id);
  const node = row?.querySelector<HTMLElement>('.message') || row;
  if (!node) return;
  const rect = node.getBoundingClientRect();
  const viewport = readingBounds(scroller);
  scroller.scrollTop +=
    pinned.edge === 'start'
      ? rect.top - viewport.top - pinned.offset
      : rect.bottom - viewport.bottom + pinned.offset;
  if (lastBoundary?.id === pinned.id && lastBoundary.edge === pinned.edge)
    lastBoundary.top = scroller.scrollTop;
}

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
    if (pinned) alignPinned();
    else if (props.following) scroller.scrollTop = scroller.scrollHeight;
    else if (virtual.value && correction) scroller.scrollTop += correction;
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
/** `offset` keeps the edge that far inside the reading bounds, e.g. where the reader already was. */
async function jumpTo(id: string, edge: 'start' | 'end' = 'start', offset = 16) {
  const index = indices.value.get(id);
  const scroller = props.scroller;
  if (index === undefined || !scroller) return;
  const version = ++jumpVersion;
  pinned = { id, edge, offset };
  for (let attempt = 0; attempt < 3; attempt++) {
    if (disposed || version !== jumpVersion) return;
    if (virtual.value && !nodes.has(id)) scroller.scrollTop = origin() + heights.offset(index);
    alignPinned();
    updateViewport();
    await nextTick();
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
  }
}
async function jumpBoundary(edge: 'start' | 'end') {
  const scroller = props.scroller;
  if (!scroller || !props.messages.length) return;
  const remembered =
    lastBoundary && indices.value.has(lastBoundary.id) && Math.abs(lastBoundary.top - scroller.scrollTop) < 3
      ? lastBoundary
      : undefined;
  let index = remembered
    ? indices.value.get(remembered.id)!
    : heights.indexAt(Math.max(0, scroller.scrollTop - origin() + 24));
  if (!virtual.value && !remembered) {
    const top = scroller.getBoundingClientRect().top + 24;
    index = props.messages.findIndex(
      (message) => (nodes.get(message.id)?.getBoundingClientRect().bottom || 0) > top,
    );
    if (index < 0) index = props.messages.length - 1;
  }
  const row = nodes.get(props.messages[index].id);
  const rect = (row?.querySelector('.message') || row)?.getBoundingClientRect();
  const bounds = readingBounds(scroller);
  const atEdge =
    remembered?.edge === edge ||
    (rect && (edge === 'start' ? rect.top >= bounds.top + 13 : rect.bottom <= bounds.bottom - 13));
  if (atEdge) index = Math.max(0, Math.min(props.messages.length - 1, index + (edge === 'start' ? -1 : 1)));
  const id = props.messages[index].id;
  await jumpTo(id, edge);
  if (pinned?.id === id && pinned.edge === edge) lastBoundary = { id, edge, top: scroller.scrollTop };
}
watch(
  () => [props.messages, props.messages.length],
  async () => {
    // Branch changes and deleted messages must release retained components and caches.
    const ids = indices.value;
    retained.value = new Set([...retained.value].filter((id) => ids.has(id)));
    for (const id of interactiveChecks.keys()) if (!ids.has(id)) interactiveChecks.delete(id);
    for (const id of measured.keys()) if (!ids.has(id)) measured.delete(id);
    if (pinned && !indices.value.has(pinned.id)) releaseAnchor();
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
    if (!observer) {
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
          checked = {
            text,
            interactive: !!message.presentation?.body.some((part) => part.format === 'html'),
          };
          interactiveChecks.set(message.id, checked);
        }
        if (checked.interactive) next.add(message.id);
      }
      if (next.size !== retained.value.size) retained.value = next;
    } else if (retained.value.size) retained.value = new Set();
    emit('visible', messages);
  },
  { immediate: true },
);
watch(
  () => props.scroller,
  (scroller, previous) => {
    previous?.removeEventListener('scroll', scheduleViewport);
    if (previous) observer?.unobserve(previous);
    for (const event of ['wheel', 'touchstart', 'pointerdown', 'keydown']) {
      previous?.removeEventListener(event, releaseAnchor);
      scroller?.addEventListener(event, releaseAnchor, { passive: true });
    }
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
  for (const event of ['wheel', 'touchstart', 'pointerdown', 'keydown'])
    props.scroller?.removeEventListener(event, releaseAnchor);
  window.removeEventListener('resize', scheduleViewport);
});
defineExpose({ jumpTo, jumpBoundary, releaseAnchor, updateViewport });
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
