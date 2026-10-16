<script setup lang="ts">
import Icon from '../common/Icon.vue';
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { historyRounds, historySummary, type HistoryMessage } from '../../lib/storyHistory';
const props = defineProps<{ messages: HistoryMessage[]; readingId: string; breakpointId?: string | null }>();
const emit = defineEmits<{ navigate: [id: string] }>();
const rail = ref<HTMLElement>();
const wrap = ref<HTMLElement>();
const height = ref(400);
const previewBox = ref<HTMLElement>();
const previewHeight = ref(300);
const previewAnchor = ref(0);
const keyboardIndex = ref<number | null>(null);
const hovered = ref<number | null>(null);
const dragging = ref(false);
const rounds = computed(() => historyRounds(props.messages));
const roundIndices = computed(
  () => new Map(rounds.value.flatMap((round, index) => round.messageIds.map((id) => [id, index] as const))),
);
const current = computed(() => roundIndices.value.get(props.readingId) || 0);
const cached = computed(() => (props.breakpointId ? roundIndices.value.get(props.breakpointId) : undefined));
const railHeight = computed(() => Math.min(height.value, Math.max(24, (rounds.value.length - 1) * 12 + 24)));
const span = computed(() => Math.max(0, railHeight.value - 24));
const count = computed(() => Math.min(rounds.value.length, Math.max(2, Math.floor(span.value / 12) + 1)));
const preview = computed(() => (hovered.value === null ? undefined : rounds.value[hovered.value]));
const nearby = computed(() => {
  if (hovered.value === null) return [];
  const start = Math.max(0, Math.min(rounds.value.length - 5, hovered.value - 2));
  return rounds.value.slice(start, start + 5).map((round, offset) => ({
    ...round,
    index: start + offset,
    summary: historySummary(round.user) || '故事开场',
    replySummary: historySummary(round.reply) || '等待 AI 回复…',
  }));
});
const fraction = (index: number) => (rounds.value.length <= 1 ? 0.5 : index / (rounds.value.length - 1));
const percent = (index: number) =>
  (((railHeight.value - span.value) / 2 + fraction(index) * span.value) / railHeight.value) * 100;
const previewTop = computed(() =>
  Math.max(
    0,
    Math.min(
      height.value - previewHeight.value,
      (height.value - span.value) / 2 + fraction(previewAnchor.value) * span.value - previewHeight.value / 2,
    ),
  ),
);
const clampIndex = (index: number) => Math.max(0, Math.min(rounds.value.length - 1, index));
let frame = 0;
let pendingIndex: number | null = null;
let lastDragId: string | undefined;
let lastPointerY: number | null = null;
let closeTimer: ReturnType<typeof setTimeout> | undefined;
function keepOpen() {
  clearTimeout(closeTimer);
}
function closePreview() {
  keepOpen();
  hovered.value = null;
  keyboardIndex.value = null;
  lastPointerY = null;
}
function leave() {
  if (!dragging.value && !wrap.value?.contains(document.activeElement))
    closeTimer = setTimeout(closePreview, 180);
}
function blur(event: FocusEvent) {
  if (!wrap.value?.contains(event.relatedTarget as Node | null)) closePreview();
}
function outside(event: PointerEvent) {
  if (!wrap.value?.contains(event.target as Node)) closePreview();
}
function focus() {
  keepOpen();
  keyboardIndex.value = null;
  lastPointerY = null;
  hovered.value = current.value;
  previewAnchor.value = current.value;
}
function dismiss() {
  rail.value?.focus({ preventScroll: true });
  closePreview();
}
function cancelNavigation() {
  cancelAnimationFrame(frame);
  frame = 0;
  pendingIndex = null;
}
function flushNavigation() {
  frame = 0;
  const round = pendingIndex === null ? undefined : rounds.value[pendingIndex];
  pendingIndex = null;
  if (round && round.id !== lastDragId) {
    lastDragId = round.id;
    emit('navigate', round.id);
  }
}
function queueNavigation(index: number) {
  pendingIndex = index;
  if (!frame) frame = requestAnimationFrame(flushNavigation);
}
function indexAt(event: PointerEvent) {
  const rect = rail.value!.getBoundingClientRect();
  return Math.max(
    0,
    Math.min(
      rounds.value.length - 1,
      span.value
        ? Math.round(
            ((event.clientY - rect.top - (railHeight.value - span.value) / 2) / span.value) *
              (rounds.value.length - 1),
          )
        : 0,
    ),
  );
}
function move(event: PointerEvent) {
  keepOpen();
  // Moving horizontally into the preview must not undo a precise wheel selection.
  if (event.clientY === lastPointerY && hovered.value !== null && !dragging.value) return;
  lastPointerY = event.clientY;
  keyboardIndex.value = null;
  hovered.value = indexAt(event);
  previewAnchor.value = hovered.value;
  if (dragging.value) queueNavigation(hovered.value);
}
function navigate(index: number) {
  cancelNavigation();
  const round = rounds.value[clampIndex(index)];
  if (round) emit('navigate', round.id);
}
function down(event: PointerEvent) {
  if (!event.isPrimary || event.button !== 0) return;
  rail.value?.focus({ preventScroll: true });
  keepOpen();
  lastDragId = undefined;
  dragging.value = true;
  rail.value?.setPointerCapture(event.pointerId);
  move(event);
}
function up(event: PointerEvent) {
  if (!dragging.value) return;
  // Flush the release position even when no animation frame ran after the last move.
  cancelNavigation();
  hovered.value = indexAt(event);
  pendingIndex = hovered.value;
  flushNavigation();
  dragging.value = false;
  if (rail.value?.hasPointerCapture(event.pointerId)) rail.value.releasePointerCapture(event.pointerId);
}
function cancelDrag() {
  dragging.value = false;
  cancelNavigation();
}
function step(delta: number) {
  keepOpen();
  keyboardIndex.value = null;
  if (hovered.value === null) previewAnchor.value = current.value;
  hovered.value = clampIndex((hovered.value ?? current.value) + delta);
}
function wheel(event: WheelEvent) {
  if (event.deltaY) step(Math.sign(event.deltaY));
}
function select(index: number) {
  hovered.value = index;
  navigate(index);
}
function key(event: KeyboardEvent) {
  lastPointerY = null;
  let index = keyboardIndex.value ?? current.value;
  if (event.key === 'Home') index = 0;
  else if (event.key === 'End') index = rounds.value.length - 1;
  else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') index--;
  else if (event.key === 'ArrowDown' || event.key === 'ArrowRight') index++;
  else if (event.key === 'Escape') {
    event.preventDefault();
    dismiss();
    return;
  } else if (event.key === 'Enter' || event.key === ' ') index = hovered.value ?? index;
  else return;
  event.preventDefault();
  keepOpen();
  keyboardIndex.value = clampIndex(index);
  hovered.value = keyboardIndex.value;
  previewAnchor.value = hovered.value;
  navigate(hovered.value);
}
// Reading can change through the prose, search, or other navigation controls.
// A hover preview is never the starting point for a new arrow-key sequence.
watch(current, () => {
  keyboardIndex.value = null;
});
watch([hovered, previewHeight], async () => {
  await nextTick();
  const list = previewBox.value?.querySelector<HTMLElement>('.rail-nearby');
  const selected = list?.querySelector<HTMLElement>('.selected');
  if (!list || !selected) return;
  const bounds = list.getBoundingClientRect();
  const row = selected.getBoundingClientRect();
  if (row.top < bounds.top) list.scrollTop += row.top - bounds.top;
  else if (row.bottom > bounds.bottom) list.scrollTop += row.bottom - bounds.bottom;
});
watch(rounds, () => {
  cancelDrag();
  if (hovered.value !== null) hovered.value = clampIndex(hovered.value);
  if (!rounds.value.length) closePreview();
});
let observer: ResizeObserver | undefined;
watch(
  [wrap, previewBox],
  ([container, panel], [oldContainer, oldPanel]) => {
    if (oldContainer) observer?.unobserve(oldContainer);
    if (oldPanel) observer?.unobserve(oldPanel);
    if (container) observer?.observe(container);
    if (panel) observer?.observe(panel);
  },
  { flush: 'post' },
);
onMounted(() => {
  observer = new ResizeObserver(() => {
    if (wrap.value?.clientHeight) height.value = wrap.value.clientHeight;
    if (previewBox.value?.offsetHeight) previewHeight.value = previewBox.value.offsetHeight;
  });
  if (wrap.value) observer.observe(wrap.value);
  if (previewBox.value) observer.observe(previewBox.value);
  document.addEventListener('pointerdown', outside);
});
onBeforeUnmount(() => {
  observer?.disconnect();
  cancelNavigation();
  keepOpen();
  document.removeEventListener('pointerdown', outside);
});
</script>

<template>
  <div
    v-if="rounds.length"
    ref="wrap"
    class="conversation-rail-wrap"
    @pointerenter="keepOpen"
    @pointerleave="leave"
    @focusout="blur"
    @keydown.esc.stop.prevent="dismiss"
  >
    <div
      ref="rail"
      class="conversation-rail"
      :style="{ height: railHeight + 'px' }"
      role="slider"
      tabindex="0"
      aria-label="对话历史导航"
      aria-orientation="vertical"
      aria-description="点击或拖动快速定位；滚轮逐轮预览，回车跳转；方向键逐轮跳转。"
      :aria-valuemin="1"
      :aria-valuemax="rounds.length"
      :aria-valuenow="current + 1"
      :aria-valuetext="`第 ${current + 1} / ${rounds.length} 个对话位置`"
      @pointermove="move"
      @pointerdown.prevent="down"
      @pointerup="up"
      @pointercancel="cancelDrag"
      @lostpointercapture="cancelDrag"
      @wheel.stop.prevent="wheel"
      @keydown="key"
      @focus="focus"
    >
      <span
        v-for="tick in count"
        :key="tick"
        class="rail-tick"
        :style="{ top: percent(count <= 1 ? 0 : ((tick - 1) / (count - 1)) * (rounds.length - 1)) + '%' }"
      />
      <span class="rail-tick current" title="当前阅读位置" :style="{ top: percent(current) + '%' }" />
      <span
        v-if="cached !== undefined"
        class="rail-tick cached"
        title="计划缓存断点"
        :style="{ top: percent(cached) + '%' }"
      />
      <span v-if="hovered !== null" class="rail-tick hovered" :style="{ top: percent(hovered) + '%' }" />
    </div>
    <div
      v-if="preview"
      ref="previewBox"
      class="rail-preview"
      role="region"
      aria-label="附近对话轮次"
      :style="{ top: previewTop + 'px', maxHeight: height + 'px' }"
      @wheel.stop.prevent="wheel"
    >
      <div class="rail-preview-heading">
        <small>{{ (hovered ?? 0) + 1 }} / {{ rounds.length }}</small>
        <span>滚轮逐轮预览 · 点击跳转</span>
        <button type="button" aria-label="关闭轮次预览" title="关闭" @click="dismiss">
          <Icon name="close" :size="14" />
        </button>
      </div>
      <div class="rail-nearby">
        <button
          v-for="round in nearby"
          :key="round.id"
          type="button"
          :class="{ selected: round.index === hovered }"
          :aria-label="`跳转到第 ${round.index + 1} 个对话位置`"
          :aria-current="round.index === current ? 'location' : undefined"
          @click="select(round.index)"
        >
          <small>{{ round.index + 1 }}</small>
          <span
            ><strong>{{ round.summary }}</strong
            ><span class="rail-reply">{{ round.replySummary }}</span></span
          >
          <span class="rail-round-status">{{
            [round.index === current ? '当前' : '', round.index === cached ? '缓存' : '']
              .filter(Boolean)
              .join(' · ')
          }}</span>
        </button>
      </div>
      <div class="rail-preview-controls">
        <button type="button" :disabled="hovered === 0" aria-label="预览上一轮" @click="step(-1)">
          <Icon name="chevron-up" :size="14" />上一轮
        </button>
        <button
          type="button"
          :disabled="hovered === rounds.length - 1"
          aria-label="预览下一轮"
          @click="step(1)"
        >
          <Icon name="chevron-down" :size="14" />下一轮
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.conversation-rail-wrap {
  position: absolute;
  right: 6px;
  top: 32px;
  bottom: calc(var(--composer-height, 0px) + 32px);
  width: 30px;
  z-index: 9;
  pointer-events: none;
}
.conversation-rail {
  position: absolute;
  top: 50%;
  left: 0;
  right: 0;
  transform: translateY(-50%);
  cursor: pointer;
  touch-action: none;
  pointer-events: auto;
  outline-offset: 2px;
}
.rail-tick {
  position: absolute;
  right: 8px;
  width: 6px;
  height: 2px;
  border-radius: 1px;
  background: rgb(255 255 255 / 0.18);
  transform: translateY(-50%);
  pointer-events: none;
  transition: background var(--dur);
}
.conversation-rail:hover .rail-tick:not(.current):not(.cached):not(.hovered) {
  background: rgb(255 255 255 / 0.3);
}
.rail-tick:nth-child(5n) {
  width: 11px;
}
/* Current, cached and hovered markers keep separate horizontal lanes. */
.rail-tick.current {
  right: 19px;
  width: 7px;
  height: 9px;
  border-radius: 0;
  background: var(--accent);
  clip-path: polygon(0 0, 100% 50%, 0 100%);
  filter: drop-shadow(0 0 4px var(--accent-glow));
}
.rail-tick.cached {
  right: 0;
  width: 4px;
  height: 8px;
  border-radius: 2px;
  background: var(--cache-gold);
}
.rail-tick.hovered {
  right: 8px;
  width: 9px;
  height: 9px;
  border: 1.5px solid var(--text-primary);
  border-radius: 50%;
  background: var(--glass-float);
}
.rail-preview {
  position: absolute;
  right: 30px;
  width: min(360px, calc(100vw - 60px));
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--glass-edge-strong);
  border-radius: var(--radius-lg);
  background: var(--glass-sheen), var(--glass-float);
  -webkit-backdrop-filter: var(--glass-blur-strong);
  backdrop-filter: var(--glass-blur-strong);
  box-shadow: var(--glass-highlight), var(--shadow-pop);
  font-size: 12px;
  line-height: 1.5;
  pointer-events: auto;
}
.rail-preview-heading,
.rail-preview-controls {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 10px;
  flex-shrink: 0;
  color: var(--text-muted);
}
.rail-preview-heading > span {
  font-size: 10px;
}
.rail-preview button {
  font: inherit;
  color: var(--text-primary);
  cursor: pointer;
}
.rail-preview button:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: -2px;
}
.rail-preview-heading button,
.rail-preview-controls button {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  min-height: 28px;
  padding: 2px 8px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
}
.rail-preview-heading button > .icon,
.rail-preview-controls button > .icon {
  margin: 0;
}
.rail-preview button:disabled {
  opacity: 0.4;
  cursor: default;
}
.rail-nearby {
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
}
.rail-nearby > button {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  min-height: 48px;
  padding: 6px 10px;
  border: 0;
  border-radius: 0;
  text-align: left;
  background: transparent;
}
.rail-nearby > button.selected,
.rail-preview button:hover:not(:disabled) {
  background: var(--hover-wash);
}
.rail-nearby > button[aria-current='location'] small {
  color: var(--accent-hover);
}
.rail-nearby small {
  flex: 0 0 4ch;
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}
.rail-nearby > button > span:first-of-type {
  flex: 1;
  min-width: 0;
}
.rail-nearby strong,
.rail-reply {
  display: block;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.rail-reply {
  color: var(--text-muted);
  font-size: 11px;
}
.rail-round-status {
  flex-shrink: 0;
  color: var(--text-faint);
  font-size: 10px;
}
@media (max-width: 620px) {
  .conversation-rail-wrap {
    right: 1px;
    width: 24px;
  }
  .rail-preview {
    right: 24px;
  }
}
</style>
