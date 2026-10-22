<script setup lang="ts">
import { useTimedError } from '../../composables/useTimedError';
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { api, rawApi } from '../../lib/api';
import WorkData from './WorkData.vue';
import { mergeWorkTurns } from '../../lib/workRecords';

const props = defineProps<{ sessionId: string; turnId?: string; events: any[]; agentMode?: string }>();
const isFullAgent = computed(() => props.agentMode === 'full_agent');
const heading = computed(() => (isFullAgent.value ? 'Agent 工作记录 · Debug' : '后台工作记录'));
const turns = ref<any[]>([]),
  next = ref<string | null>(null),
  loading = ref(false),
  error = ref('');
const records = ref<Record<string, { items: any[]; next: number | null }>>({});
const payloads = ref<Record<number, any>>({});
const payloadErrors = ref<Record<number, string>>({});
const fetching = ref(new Set<string>()),
  openedTurns = ref(new Set<string>()),
  openedEvents = ref(new Set<number>());
const category = ref('all');
const expanded = ref(false);
const rawEvents = ref(new Set<number>());
let generation = 0,
  timer: ReturnType<typeof setTimeout> | undefined;
const purposes: Record<string, string> = {
  full_agent: 'Full Agent',
  memory: '记忆整理',
  memory_review: '疑难复核',
  memory_recall: '归档召回筛选',
  variables: 'MVU 变量',
  maintenance: '设定维护',
  tracker_pre: 'Tracker · 正文前',
  tracker_post: 'Tracker · 正文后',
  tracker_rebuild: 'Tracker · 记忆重建',
  runtime: '回合处理',
};
const statuses: Record<string, string> = {
  queued: '排队中',
  running: '运行中',
  awaiting_review: '待审核',
  committed: '已完成',
  error: '失败',
  cancelled: '已取消',
  interrupted: '已中断',
};
const statusLabel = (status: string) => statuses[status] || status;
function title(turn: any) {
  if (isFullAgent.value)
    return turn.mode === 'regenerate' ? 'Full Agent · 重新生成' : 'Full Agent · 对话回合';
  return turn.kind === 'memory'
    ? '重新生成记忆'
    : turn.kind === 'maintenance'
      ? '设定维护与后台任务'
      : turn.mode === 'regenerate'
        ? '重生成中的后台任务'
        : '发送消息后的后台任务';
}
function time(value: number) {
  return new Date(value * 1000).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}
function filtered(turnId: string) {
  return (records.value[turnId]?.items || [])
    .filter((e) => category.value === 'all' || e.purpose === category.value)
    .sort((a, b) => a.seq - b.seq);
}
const total = computed(() => turns.value.length);

async function load(more = false) {
  if (loading.value) return;
  const sid = props.sessionId,
    version = generation;
  loading.value = true;
  error.value = '';
  try {
    const result = await api(
      `/sessions/${sid}/work-records${more && next.value ? `?before=${next.value}` : ''}`,
    );
    if (version !== generation) return;
    turns.value = mergeWorkTurns(turns.value, result.items);
    if (more || turns.value.length <= 20) next.value = result.next;
    for (const turn of turns.value) if (openedTurns.value.has(turn.id)) await loadEvents(turn.id);
  } catch (e: any) {
    if (version === generation) error.value = e.message;
  } finally {
    if (version === generation) loading.value = false;
  }
}
async function loadEvents(tid: string, more = false) {
  if (fetching.value.has(tid)) return;
  const version = generation,
    sid = props.sessionId;
  fetching.value.add(tid);
  try {
    const previous = records.value[tid];
    const after = more ? previous?.next || 0 : previous?.items.at(-1)?.seq || 0;
    const result = await api(`/sessions/${sid}/work-records/${tid}/events?after=${after}`);
    if (version !== generation) return;
    const items = [
      ...(previous?.items || []),
      ...result.items.filter((e: any) => !previous?.items.some((old) => old.seq === e.seq)),
    ];
    records.value[tid] = { items, next: result.next };
  } catch (e: any) {
    if (version === generation) error.value = e.message;
  } finally {
    if (version === generation) fetching.value.delete(tid);
  }
}
function toggleTurn(event: Event, tid: string) {
  if ((event.currentTarget as HTMLDetailsElement).open) {
    openedTurns.value.add(tid);
    void loadEvents(tid);
  } else openedTurns.value.delete(tid);
}
async function toggleEvent(event: Event, tid: string, seq: number) {
  if (!(event.currentTarget as HTMLDetailsElement).open) {
    openedEvents.value.delete(seq);
    return;
  }
  openedEvents.value.add(seq);
  await loadPayload(tid, seq);
}
async function loadPayload(tid: string, seq: number) {
  if (Object.hasOwn(payloads.value, seq) || fetching.value.has(String(seq))) return;
  const version = generation,
    sid = props.sessionId;
  payloadErrors.value = { ...payloadErrors.value, [seq]: '' };
  fetching.value.add(String(seq));
  try {
    const value = await rawApi(`/sessions/${sid}/work-records/${tid}/events/${seq}`);
    if (version === generation) payloads.value = { ...payloads.value, [seq]: value };
  } catch (e: any) {
    if (version === generation) payloadErrors.value = { ...payloadErrors.value, [seq]: e.message };
  } finally {
    if (version === generation) fetching.value.delete(String(seq));
  }
}
function foldAll() {
  expanded.value = false;
  openedTurns.value.clear();
  openedEvents.value.clear();
}
function expandAll() {
  expanded.value = true;
  for (const turn of turns.value) {
    openedTurns.value.add(turn.id);
    void loadEvents(turn.id);
  }
}
watch(
  () => props.sessionId,
  () => {
    generation++;
    clearTimeout(timer);
    turns.value = [];
    records.value = {};
    payloads.value = {};
    payloadErrors.value = {};
    rawEvents.value.clear();
    next.value = null;
    loading.value = false;
    fetching.value.clear();
    foldAll();
    category.value = 'all';
    void load();
  },
  { immediate: true },
);
watch(
  () => [props.turnId, props.events.at(-1)?.seq],
  () => {
    clearTimeout(timer);
    timer = setTimeout(() => void load(), 400);
  },
);
onBeforeUnmount(() => {
  generation++;
  clearTimeout(timer);
});
useTimedError(error);
</script>

<template>
  <section class="work-records" :aria-label="heading">
    <div class="work-toolbar">
      <div>
        <h2>{{ heading }}</h2>
        <p>
          {{
            isFullAgent ? '模型请求与响应、工具数据、用量及运行时事件。' : '记忆、维护工具与回合处理。'
          }}按时间从旧到新，展开查看结构化数据或原始 JSON。
        </p>
      </div>
      <div class="work-controls">
        <select v-model="category" aria-label="记录类型">
          <option value="all">全部记录</option>
          <option v-for="(label, key) in purposes" :key="key" :value="key">{{ label }}</option></select
        ><button @click="expanded ? foldAll() : expandAll()">{{ expanded ? '收起回合' : '展开回合' }}</button
        ><button :disabled="loading" @click="load()">{{ loading ? '加载中…' : '刷新' }}</button>
      </div>
    </div>
    <AppNotification :message="error" />
    <p v-if="!total" class="work-empty">
      {{ loading ? '正在读取工作记录…' : '还没有工作记录。发送消息或手动更新记忆后，记录会保存在这里。' }}
    </p>
    <button v-if="next" class="work-load-more" :disabled="loading" @click="load(true)">
      {{ loading ? '加载中…' : '加载更早回合' }}
    </button>
    <details
      v-for="turn in turns"
      :key="turn.id"
      class="work-turn"
      :open="openedTurns.has(turn.id)"
      @toggle="toggleTurn($event, turn.id)"
    >
      <summary>
        <span class="work-status" :class="turn.status">{{ statusLabel(turn.status) }}</span
        ><strong>{{ title(turn) }}</strong
        ><span class="work-turn-preview" :title="turn.text">{{ turn.text }}</span
        ><time>{{ time(turn.created) }}</time>
      </summary>
      <div v-if="openedTurns.has(turn.id)" class="work-events">
        <p class="work-debug-meta">
          Turn <code>{{ turn.id }}</code> · {{ turn.mode }} · {{ turn.status }}
        </p>
        <p v-if="turn.error" class="work-error">{{ turn.error }}</p>
        <p v-if="!records[turn.id]" class="work-empty">正在读取本回合记录…</p>
        <details
          v-for="event in filtered(turn.id)"
          :key="event.seq"
          class="work-event"
          :open="openedEvents.has(event.seq)"
          @toggle="toggleEvent($event, turn.id, event.seq)"
        >
          <summary>
            <span class="work-purpose">{{ purposes[event.purpose] || event.purpose }}</span
            ><strong>{{ event.label }}</strong
            ><span class="work-event-preview" :title="event.description">{{ event.description }}</span
            ><small>{{
              event.bytes >= 1024 ? `${(event.bytes / 1024).toFixed(1)} KiB` : `${event.bytes} B`
            }}</small
            ><time>{{ time(event.created) }}</time>
          </summary>
          <div v-if="openedEvents.has(event.seq)" class="work-payload">
            <div class="work-debug-meta">
              <code>#{{ event.seq }} · {{ event.kind }}</code>
              <button
                v-if="Object.hasOwn(payloads, event.seq)"
                @click="rawEvents.has(event.seq) ? rawEvents.delete(event.seq) : rawEvents.add(event.seq)"
              >
                {{ rawEvents.has(event.seq) ? '结构化查看' : '原始 JSON' }}
              </button>
            </div>
            <WorkData
              v-if="Object.hasOwn(payloads, event.seq) && !rawEvents.has(event.seq)"
              :value="payloads[event.seq]"
              label="记录数据"
              root
            />
            <pre v-else-if="Object.hasOwn(payloads, event.seq)" class="work-raw-json">{{
              JSON.stringify(payloads[event.seq], null, 2)
            }}</pre>
            <AppNotification v-else-if="payloadErrors[event.seq]" :message="payloadErrors[event.seq]" />
            <p v-else class="work-empty">正在读取数据…</p>
          </div>
        </details>
        <p v-if="records[turn.id] && !filtered(turn.id).length" class="work-empty">
          {{
            category === 'all' ? '本回合尚无后台工作事件。' : '已加载记录中没有此类型；可继续加载或切换类型。'
          }}
        </p>
        <button
          v-if="records[turn.id]?.next"
          class="work-load-more"
          :disabled="fetching.has(turn.id)"
          @click="loadEvents(turn.id, true)"
        >
          继续加载本回合事件
        </button>
      </div>
    </details>
  </section>
</template>

<style scoped>
.work-debug-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin: 8px 0;
  color: var(--text-muted);
  font: var(--fs-xs) / 1.6 var(--font-ui);
  overflow-wrap: anywhere;
}
.work-debug-meta button {
  margin-left: auto;
  font-size: var(--fs-xs);
}
.work-raw-json {
  max-height: 520px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font: var(--fs-sm) / 1.7 monospace;
}
</style>
