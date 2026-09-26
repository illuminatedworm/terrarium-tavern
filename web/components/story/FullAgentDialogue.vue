<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { api } from '../../lib/api';
import { liveActivity, mergeActivity, type AgentActivity } from '../../lib/agentActivity';
import AgentActivityItem from './AgentActivityItem.vue';

const props = defineProps<{
  sessionId: string;
  messages: any[];
  turn?: any;
  events: any[];
  pending?: boolean;
}>();
const emit = defineEmits<{ contentChange: [] }>();
const records = ref<Record<string, AgentActivity[]>>({});
const pendingRecords = ref<AgentActivity[]>([]);
const error = ref('');
let generation = 0;
let pendingGeneration = 0;
const prefix = computed(() => `/sessions/${props.sessionId}/full-agent`);
const activeItems = computed(() =>
  mergeActivity(pendingRecords.value, liveActivity(props.events, props.turn?.id, props.turn?.status)),
);
const pendingLabel = computed(
  () =>
    (
      ({
        awaiting_review: '本回合等待审核，工具修改尚未提交。',
        error: '本回合失败，以下工具记录属于未提交的尝试。',
        cancelled: '本回合已取消，以下工具修改未提交。',
        interrupted: '本回合已中断，以下工具修改未提交。',
      }) as Record<string, string>
    )[props.turn?.status] || '',
);

async function loadMessages() {
  const version = ++generation;
  const missing = props.messages
    .filter((m) => m.role === 'assistant' && !Object.hasOwn(records.value, m.id))
    .map((m) => m.id);
  error.value = '';
  try {
    for (let offset = 0; offset < missing.length; offset += 100) {
      const result = await api(`${prefix.value}/activity`, {
        message_ids: missing.slice(offset, offset + 100),
      });
      if (version !== generation) return;
      records.value = { ...records.value, ...result.messages };
    }
    if (version === generation) emit('contentChange');
  } catch (e: any) {
    if (version === generation) error.value = `工具记录加载失败：${e.message}`;
  }
}
async function loadPending() {
  const version = ++pendingGeneration;
  pendingRecords.value = [];
  if (!props.pending || !props.turn?.id) return;
  try {
    const result = await api(`${prefix.value}/turns/${props.turn.id}/activity`);
    if (version !== pendingGeneration) return;
    pendingRecords.value = result.items;
    emit('contentChange');
  } catch (e: any) {
    if (version === pendingGeneration) error.value = `工具记录加载失败：${e.message}`;
  }
}
function retry() {
  void loadMessages();
  void loadPending();
}
watch(
  () => props.messages.map((m) => m.id).join(','),
  () => void loadMessages(),
  { immediate: true },
);
watch(
  () => [props.turn?.id, props.turn?.status, props.pending],
  () => void loadPending(),
  { immediate: true },
);
watch(
  () => props.events.at(-1)?.seq,
  () => emit('contentChange'),
  { flush: 'post' },
);
onBeforeUnmount(() => {
  generation++;
  pendingGeneration++;
});
</script>

<template>
  <div class="full-agent-dialogue">
    <p v-if="error" class="agent-dialogue-notice" role="alert">
      {{ error }} <button @click="retry">重试</button>
    </p>
    <template v-for="message in messages" :key="message.id">
      <slot name="before-message" :message="message" />
      <div
        v-if="records[message.id]?.length"
        class="agent-message-activity"
        :data-for-message="message.id"
        aria-label="本条回复的工具与过程"
      >
        <AgentActivityItem
          v-for="item in records[message.id]"
          :key="item.id"
          :item="item"
          :endpoint="`${prefix}/messages/${message.id}/activity`"
        />
      </div>
      <slot name="message" :message="message" />
    </template>
    <slot name="pending" />
    <div
      v-if="pending && activeItems.length"
      class="agent-pending-activity"
      :data-turn-id="turn.id"
      aria-label="当前回合的工具与过程"
    >
      <p v-if="pendingLabel" class="agent-dialogue-notice">{{ pendingLabel }}</p>
      <AgentActivityItem
        v-for="item in activeItems"
        :key="`${turn.id}:${item.id}`"
        :item="item"
        :endpoint="`${prefix}/turns/${turn.id}/activity`"
      />
    </div>
    <slot name="draft" />
    <slot name="waiting" />
  </div>
</template>

<style scoped>
.agent-dialogue-notice {
  margin: 12px 0 16px 52px;
  color: var(--text-muted);
  font: 12px/1.7 var(--font-ui);
}
@media (max-width: 620px) {
  .agent-dialogue-notice {
    margin-left: 0;
  }
}
</style>
