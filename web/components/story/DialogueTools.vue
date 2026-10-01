<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue';
import { renderStoryText } from '../../lib/storyText';
import { storyHtml } from '../../lib/storyHtml';
import { textMatches } from '../../lib/dialogueSearch';
import type { HistoryMessage } from '../../lib/storyHistory';
const props = defineProps<{
  messages: HistoryMessage[];
  persona?: string;
  scroller?: HTMLElement;
  active: boolean;
  navigate: (id: string) => Promise<void>;
  releaseAnchor: () => void;
}>();
const emit = defineEmits<{ boundary: [edge: 'start' | 'end'] }>();
const open = ref(false);
const input = ref<HTMLInputElement>();
const query = ref('');
const debounced = ref('');
const selected = ref(0);
const locating = ref(false);
let previousFocus: HTMLElement | null = null;
let timer: ReturnType<typeof setTimeout>;
let version = 0;
let observer: MutationObserver | undefined;
let resizeObserver: ResizeObserver | undefined;
let observedRow: Element | undefined;
let pinnedResult = false;
let frame = 0;
const cache = new Map<string, { source: string; text: string }>();
const indexed = computed(() => {
  if (!open.value) return [];
  const ids = new Set(props.messages.map((message) => message.id));
  for (const id of cache.keys()) if (!ids.has(id)) cache.delete(id);
  return props.messages.map((message) => {
    const raw = (message.display || message.raw || '').replace(
      /\{\{user\}\}/g,
      () => props.persona || '旅人',
    );
    const source = storyHtml(raw) || raw;
    let entry = cache.get(message.id);
    if (!entry || entry.source !== source) {
      const root = document.createElement('div');
      root.innerHTML = renderStoryText(source, message.role === 'user');
      root.querySelectorAll('script, style, template').forEach((node) => node.remove());
      entry = { source, text: root.textContent || '' };
      cache.set(message.id, entry);
    }
    return { id: message.id, text: entry.text };
  });
});
const results = computed(() =>
  indexed.value.flatMap((message) =>
    textMatches(message.text, debounced.value).map((match, occurrence) => ({
      ...match,
      ...message,
      occurrence,
    })),
  ),
);
const current = computed(() => results.value[selected.value]);
const snippet = computed(() => {
  const hit = current.value;
  return hit
    ? {
        before: hit.text.slice(Math.max(0, hit.start - 28), hit.start),
        match: hit.text.slice(hit.start, hit.end),
        after: hit.text.slice(hit.end, hit.end + 48),
      }
    : null;
});
// CSS highlights preserve markdown, text selection and interactive message state.
const highlights = (CSS as any).highlights;
function clearHighlights() {
  highlights?.delete('dialogue-matches');
  highlights?.delete('dialogue-current');
}
function releaseSearchPosition() {
  pinnedResult = false;
  if (locating.value) {
    version++;
    locating.value = false;
    props.releaseAnchor();
  }
}
function pointerReading(event: PointerEvent) {
  if (!(event.target instanceof Element) || !event.target.closest('.dialogue-find')) releaseSearchPosition();
}
function alignMatch(range: Range) {
  const scroller = props.scroller;
  if (!scroller) return;
  scroller.scrollTop +=
    range.getBoundingClientRect().top - scroller.getBoundingClientRect().top - scroller.clientHeight * 0.4;
}
function refreshHighlights() {
  if (!open.value) return;
  cancelAnimationFrame(frame);
  frame = requestAnimationFrame(() => {
    const range = paint();
    if (range && pinnedResult && !locating.value) alignMatch(range);
  });
}
function paint(): Range | undefined {
  clearHighlights();
  if (!open.value || !props.active || !debounced.value || !props.scroller) return;
  const all: Range[] = [],
    active: Range[] = [];
  for (const article of Array.from(props.scroller.querySelectorAll<HTMLElement>('[data-message-id]'))) {
    const prose = article.querySelector('.prose');
    if (!prose) continue;
    if (article.dataset.messageId === current.value?.id) {
      const row = article.closest('.history-row') || article;
      if (observedRow !== row) {
        if (observedRow) resizeObserver?.unobserve(observedRow);
        observedRow = row;
        resizeObserver?.observe(row);
      }
    }
    const walker = document.createTreeWalker(prose, NodeFilter.SHOW_TEXT);
    const nodes: Text[] = [];
    while (walker.nextNode()) nodes.push(walker.currentNode as Text);
    const text = nodes.map((node) => node.data).join('');
    textMatches(text, debounced.value).forEach((hit, occurrence) => {
      const range = document.createRange();
      let offset = 0;
      for (const node of nodes) {
        const end = offset + node.length;
        if (hit.start >= offset && hit.start < end) range.setStart(node, hit.start - offset);
        if (hit.end > offset && hit.end <= end) range.setEnd(node, hit.end - offset);
        offset = end;
      }
      all.push(range);
      if (current.value?.id === article.dataset.messageId && current.value.occurrence === occurrence)
        active.push(range);
    });
  }
  const Highlight = (window as any).Highlight;
  if (highlights && Highlight) {
    highlights.set('dialogue-matches', new Highlight(...all));
    highlights.set('dialogue-current', new Highlight(...active));
  }
  return active[0];
}
async function locate() {
  const generation = ++version;
  const hit = current.value;
  if (!hit || !open.value || !props.active) {
    pinnedResult = false;
    locating.value = false;
    paint();
    return;
  }
  locating.value = true;
  pinnedResult = false;
  await props.navigate(hit.id);
  if (generation !== version || !open.value) return;
  await nextTick();
  const range = paint();
  if (range) {
    props.releaseAnchor();
    pinnedResult = true;
    alignMatch(range);
  }
  locating.value = false;
}
function move(direction: number) {
  if (query.value !== debounced.value) {
    clearTimeout(timer);
    debounced.value = query.value;
    return;
  }
  if (!results.value.length) return;
  selected.value = (selected.value + direction + results.value.length) % results.value.length;
  void locate();
}
async function show() {
  if (!props.active || document.querySelector('[aria-modal="true"]')) return;
  if (!open.value) previousFocus = document.activeElement as HTMLElement;
  open.value = true;
  await nextTick();
  input.value?.focus();
  input.value?.select();
}
function close(restore = true) {
  open.value = false;
  version++;
  locating.value = false;
  pinnedResult = false;
  props.releaseAnchor();
  clearHighlights();
  if (restore) (previousFocus?.isConnected ? previousFocus : props.scroller)?.focus({ preventScroll: true });
}
function keyboard(event: KeyboardEvent) {
  if (!props.active || event.isComposing || document.querySelector('[aria-modal="true"]')) return;
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f') {
    event.preventDefault();
    void show();
  } else if (open.value && event.key === 'Escape') {
    event.preventDefault();
    close();
  }
}
window.addEventListener('keydown', keyboard);
window.addEventListener('tavern-dialogue-find', show);
window.addEventListener('pointerdown', pointerReading, true);
watch(query, () => {
  clearTimeout(timer);
  version++;
  pinnedResult = false;
  props.releaseAnchor();
  timer = setTimeout(() => {
    debounced.value = query.value;
  }, 120);
});
watch(results, () => {
  selected.value = 0;
  void locate();
});
watch(
  () => props.active,
  (active) => {
    if (!active) close(false);
  },
);
watch(
  () => props.scroller,
  (scroller, previous) => {
    observer?.disconnect();
    resizeObserver?.disconnect();
    observedRow = undefined;
    for (const event of ['wheel', 'touchstart', 'keydown']) {
      previous?.removeEventListener(event, releaseSearchPosition);
      scroller?.addEventListener(event, releaseSearchPosition, { passive: true });
    }
    observer = new MutationObserver(refreshHighlights);
    resizeObserver = new ResizeObserver(refreshHighlights);
    if (scroller) {
      observer.observe(scroller, {
        childList: true,
        subtree: true,
        characterData: true,
        attributes: true,
        attributeFilter: ['style'],
      });
      resizeObserver.observe(scroller);
    }
  },
  { immediate: true },
);
onBeforeUnmount(() => {
  version++;
  clearTimeout(timer);
  cancelAnimationFrame(frame);
  observer?.disconnect();
  resizeObserver?.disconnect();
  for (const event of ['wheel', 'touchstart', 'keydown'])
    props.scroller?.removeEventListener(event, releaseSearchPosition);
  clearHighlights();
  window.removeEventListener('keydown', keyboard);
  window.removeEventListener('tavern-dialogue-find', show);
  window.removeEventListener('pointerdown', pointerReading, true);
});
</script>

<template>
  <div v-if="active" class="dialogue-tools">
    <button
      v-if="!open"
      class="find-trigger"
      aria-label="搜索对话"
      title="搜索全部对话（Ctrl + F）"
      @click="show"
    >
      <svg viewBox="0 0 20 20" aria-hidden="true">
        <circle cx="8.5" cy="8.5" r="5.5" />
        <path d="m13 13 4 4" />
      </svg>
    </button>
    <section v-else class="dialogue-find" aria-label="对话搜索">
      <div class="find-row">
        <svg viewBox="0 0 20 20" aria-hidden="true">
          <circle cx="8.5" cy="8.5" r="5.5" />
          <path d="m13 13 4 4" />
        </svg>
        <input
          ref="input"
          v-model="query"
          aria-label="搜索全部对话"
          placeholder="搜索全部对话…"
          @keydown.enter.prevent="!$event.isComposing && move($event.shiftKey ? -1 : 1)"
        />
        <span class="find-count" role="status" aria-live="polite">{{
          results.length ? `${selected + 1} / ${results.length}` : debounced ? '无结果' : '全文'
        }}</span>
        <button
          aria-label="上一个匹配"
          title="上一个匹配（Shift + Enter）"
          :disabled="!results.length"
          @click="move(-1)"
        >
          <svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 12 5-5 5 5" /></svg>
        </button>
        <button
          aria-label="下一个匹配"
          title="下一个匹配（Enter）"
          :disabled="!results.length"
          @click="move(1)"
        >
          <svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 8 5 5 5-5" /></svg>
        </button>
        <button aria-label="关闭搜索" title="关闭（Esc）" @click="close()">
          <svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 5 10 10M5 15 15 5" /></svg>
        </button>
      </div>
      <p v-if="snippet" class="find-preview" :aria-busy="locating">
        {{ snippet.before }}<mark>{{ snippet.match }}</mark
        >{{ snippet.after }}
      </p>
      <p v-else class="find-hint">
        {{ debounced ? '未找到匹配内容，试试其他关键词' : '搜索用户和 AI 消息 · Enter 下一个 · Esc 关闭' }}
      </p>
    </section>
    <nav v-if="messages.length" class="message-jumps" aria-label="消息阅读导航">
      <button
        aria-label="跳到消息开头"
        title="跳到本条开头；再次点击到上一条"
        @click="emit('boundary', 'start')"
      >
        <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 4h10m-10 8 5-5 5 5m-5-5v10" /></svg>
      </button>
      <button
        aria-label="跳到消息结尾"
        title="跳到本条结尾；再次点击到下一条"
        @click="emit('boundary', 'end')"
      >
        <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 16h10M5 8l5 5 5-5m-5 5V3" /></svg>
      </button>
    </nav>
  </div>
</template>

<style scoped>
.dialogue-tools {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 8;
}
.dialogue-tools button,
.dialogue-find {
  pointer-events: auto;
}
.dialogue-tools svg {
  width: 17px;
  height: 17px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.5;
  stroke-linecap: round;
  stroke-linejoin: round;
  flex: none;
}
.dialogue-tools button {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  padding: 0;
  border: 0;
  border-radius: 8px;
  color: var(--text-secondary);
  background: transparent;
}
.dialogue-tools button:hover {
  background: var(--surface-hover);
  color: var(--text-primary);
}
.dialogue-tools button:focus-visible {
  outline: 2px solid var(--accent-hover);
  outline-offset: 2px;
}
.dialogue-tools button:disabled {
  opacity: 0.3;
}
.message-jumps,
.find-trigger {
  position: absolute;
  right: 39px;
  border: 1px solid var(--border-subtle) !important;
  background: var(--bg-elevated, #292929) !important;
  box-shadow: 0 4px 16px #0002;
  opacity: 0.5;
  transition: opacity 150ms;
}
.message-jumps {
  top: 50%;
  transform: translateY(-50%);
  border-radius: 12px;
  padding: 3px;
  display: grid;
  gap: 4px;
}
.message-jumps:hover,
.message-jumps:focus-within,
.find-trigger:hover,
.find-trigger:focus-visible {
  opacity: 1;
}
.find-trigger {
  top: 12px;
}
.dialogue-find {
  position: absolute;
  top: 10px;
  right: 42px;
  width: min(450px, calc(100% - 60px));
  border: 1px solid var(--border-strong);
  border-radius: 12px;
  background: var(--bg-elevated, #292929);
  box-shadow: 0 8px 32px #0004;
  color: var(--text-secondary);
  overflow: hidden;
}
.find-row {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 7px 8px 7px 12px;
}
.find-row input {
  flex: 1;
  width: 0;
  min-width: 40px;
  border: none;
  background: transparent;
  padding: 5px 2px;
  color: var(--text-primary);
  font: 13px/1.5 var(--font-ui);
  box-shadow: none;
}
.find-row input:focus {
  outline: none;
}
.dialogue-find:focus-within {
  border-color: var(--accent-hover);
}
.find-count {
  font: 11px/1.5 var(--font-ui);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.find-preview,
.find-hint {
  margin: 0;
  padding: 8px 12px 10px;
  border-top: 1px solid var(--border-subtle);
  font: 12px/1.6 var(--font-ui);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.find-hint {
  color: var(--text-muted);
}
.find-preview mark {
  background: #e6bf7040;
  color: #f3d79e;
  border-radius: 2px;
}
@media (max-width: 620px) {
  .message-jumps,
  .find-trigger {
    right: 27px;
  }
  .dialogue-tools button {
    width: 36px;
    height: 36px;
  }
  .dialogue-find {
    right: 8px;
    width: calc(100% - 16px);
  }
  .find-row {
    gap: 2px;
  }
  .find-row > svg {
    display: none;
  }
}
@media (hover: none) {
  .message-jumps,
  .find-trigger {
    opacity: 0.85;
  }
}
@media (prefers-reduced-motion: reduce) {
  .message-jumps,
  .find-trigger {
    transition: none;
  }
}
</style>
<style>
::highlight(dialogue-matches) {
  background: #d4af563b;
  color: inherit;
}
::highlight(dialogue-current) {
  background: #edc572;
  color: #24211b;
}
</style>
