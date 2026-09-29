<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { historyRounds, historySummary, type HistoryMessage } from '../../lib/storyHistory';
const props = defineProps<{ messages: HistoryMessage[]; readingId: string; breakpointId?: string | null }>();
const emit = defineEmits<{ navigate: [id: string] }>();
const rail = ref<HTMLElement>();
const height = ref(400);
const hovered = ref<number | null>(null);
const dragging = ref(false);
const rounds = computed(() => historyRounds(props.messages));
const roundIndices = computed(
  () => new Map(rounds.value.flatMap((round, index) => round.messageIds.map((id) => [id, index] as const))),
);
const current = computed(() => roundIndices.value.get(props.readingId) || 0);
const cached = computed(() => (props.breakpointId ? roundIndices.value.get(props.breakpointId) : undefined));
const count = computed(() => Math.min(rounds.value.length, Math.max(2, Math.floor(height.value / 12))));
const preview = computed(() => (hovered.value === null ? undefined : rounds.value[hovered.value]));
const percent = (index: number) =>
  rounds.value.length <= 1 ? 50 : (index / (rounds.value.length - 1)) * 100;
function indexAt(event: PointerEvent) {
  const rect = rail.value!.getBoundingClientRect();
  return Math.max(
    0,
    Math.min(
      rounds.value.length - 1,
      Math.round(((event.clientY - rect.top) / rect.height) * (rounds.value.length - 1)),
    ),
  );
}
function move(event: PointerEvent) {
  hovered.value = indexAt(event);
  if (dragging.value) navigate(hovered.value);
}
function navigate(index: number) {
  const round = rounds.value[index];
  if (round) emit('navigate', round.id);
}
function down(event: PointerEvent) {
  dragging.value = true;
  rail.value?.setPointerCapture(event.pointerId);
  move(event);
}
function up(event: PointerEvent) {
  dragging.value = false;
  if (rail.value?.hasPointerCapture(event.pointerId)) rail.value.releasePointerCapture(event.pointerId);
}
function key(event: KeyboardEvent) {
  let index = hovered.value ?? current.value;
  if (event.key === 'Home') index = 0;
  else if (event.key === 'End') index = rounds.value.length - 1;
  else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') index--;
  else if (event.key === 'ArrowDown' || event.key === 'ArrowRight') index++;
  else if (event.key === 'Escape') {
    hovered.value = null;
    return;
  } else if (event.key !== 'Enter' && event.key !== ' ') return;
  event.preventDefault();
  hovered.value = Math.max(0, Math.min(rounds.value.length - 1, index));
  navigate(hovered.value);
}
let observer: ResizeObserver | undefined;
onMounted(() => {
  observer = new ResizeObserver(() => {
    height.value = rail.value?.clientHeight || 400;
  });
  if (rail.value) observer.observe(rail.value);
});
onBeforeUnmount(() => observer?.disconnect());
</script>

<template>
  <div v-if="rounds.length" class="conversation-rail-wrap">
    <div
      ref="rail"
      class="conversation-rail"
      role="slider"
      tabindex="0"
      aria-label="对话历史导航"
      aria-orientation="vertical"
      :aria-valuemin="1"
      :aria-valuemax="rounds.length"
      :aria-valuenow="current + 1"
      :aria-valuetext="`第 ${current + 1} / ${rounds.length} 个对话位置`"
      @pointermove="move"
      @pointerdown.prevent="down"
      @pointerup="up"
      @pointercancel="up"
      @pointerleave="!dragging && (hovered = null)"
      @keydown="key"
      @blur="hovered = null"
    >
      <span
        v-for="tick in count"
        :key="tick"
        class="rail-tick"
        :style="{ top: (count <= 1 ? 50 : ((tick - 1) / (count - 1)) * 100) + '%' }"
      />
      <span class="rail-tick current" :style="{ top: percent(current) + '%' }" />
      <span v-if="cached !== undefined" class="rail-tick cached" :style="{ top: percent(cached) + '%' }" />
      <span v-if="hovered !== null" class="rail-tick hovered" :style="{ top: percent(hovered) + '%' }" />
    </div>
    <div
      v-if="preview"
      class="rail-preview"
      :style="{ top: `clamp(0px, ${percent(hovered!)}%, calc(100% - 132px))` }"
    >
      <small>{{ (hovered || 0) + 1 }} / {{ rounds.length }}</small>
      <strong>{{ historySummary(preview.user) || '故事开场' }}</strong>
      <p>{{ historySummary(preview.reply) || '等待 AI 回复…' }}</p>
    </div>
  </div>
</template>

<style scoped>
.conversation-rail-wrap {
  position: absolute;
  right: 6px;
  top: 32px;
  bottom: 32px;
  width: 30px;
  z-index: 5;
  pointer-events: none;
}
.conversation-rail {
  position: absolute;
  inset: 0;
  cursor: pointer;
  touch-action: none;
  pointer-events: auto;
  outline-offset: 2px;
}
.rail-tick {
  position: absolute;
  right: 7px;
  width: 7px;
  height: 2px;
  background: var(--border-strong);
  transform: translateY(-50%);
  pointer-events: none;
}
.rail-tick:nth-child(5n) {
  width: 13px;
}
.rail-tick.current {
  width: 24px;
  background: var(--text-primary);
}
.rail-tick.cached {
  width: 19px;
  background: #f2b928;
}
.rail-tick.hovered {
  width: 24px;
  background: var(--text-secondary);
}
.rail-preview {
  position: absolute;
  right: 38px;
  width: min(330px, calc(100vw - 90px));
  padding: 12px 15px;
  border: 1px solid var(--border-subtle);
  border-radius: 16px;
  background: var(--bg-elevated, #292929);
  box-shadow: 0 8px 28px #0003;
  font-size: 13px;
  line-height: 1.6;
}
.rail-preview small {
  float: right;
  margin-left: 8px;
  color: var(--text-faint);
  font-size: 10px;
}
.rail-preview strong {
  display: block;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--text-primary);
}
.rail-preview p {
  margin: 4px 0 0;
  color: var(--text-muted);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
@media (max-width: 620px) {
  .conversation-rail-wrap {
    right: 1px;
    width: 24px;
  }
}
</style>
