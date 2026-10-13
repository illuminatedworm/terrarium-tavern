<script setup lang="ts">
import StoryMessage from './StoryMessage.vue';
import CharacterPortrait from '../character/CharacterPortrait.vue';
import InlineMessageEditor from './InlineMessageEditor.vue';
import EditMessageButton from './EditMessageButton.vue';
import { computed } from 'vue';
const props = defineProps<{
  message: any;
  interval?: string;
  session: any;
  name: string;
  portraitSrc?: string;
  sourceHighlight?: any;
  regeneratingId?: string | null;
  busy: boolean;
  switchingVersion: boolean;
  editable: boolean;
  editingMessageId: string;
  editingNotifyChoice: boolean | null;
  savingEdit: boolean;
  reviewing: boolean;
  enabled: boolean;
  writerResponseCache: boolean;
  cacheEligible: boolean;
  cacheBreakpointId: string | null;
  cacheBreakpointBusy: boolean;
}>();
const sentAt = computed(() => {
  const value = props.message.created;
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  const date = new Date(value * 1000);
  return Number.isNaN(date.getTime()) ? null : date;
});
const editingMessageText = defineModel<string>('editingMessageText', { required: true });
const emit = defineEmits<{
  cancelEdit: [];
  toggleCacheBreakpoint: [];
  setCacheBreakpoint: [messageId: string];
  saveEdit: [message: any, notifyAi: boolean];
  edit: [message: any, notifyAi: boolean | null];
  fork: [message: any];
  switchVersion: [message: any, direction: number];
  action: [
    method: string,
    args: any,
    resolve: (value: any) => void,
    reject: (error: any) => void,
    onStream: (chunk: string) => void,
  ];
}>();
function forwardAction(
  method: string,
  args: any,
  resolve: (value: any) => void,
  reject: (error: any) => void,
  onStream: (chunk: string) => void,
) {
  emit('action', method, args, resolve, reject, onStream);
}
</script>

<template>
  <article
    :data-message-id="message.id"
    class="message"
    :class="[
      message.role,
      {
        regenerating: message.id === regeneratingId,
        'source-message-highlight': sourceHighlight?.id === message.id,
      },
    ]"
    :aria-busy="message.id === regeneratingId"
  >
    <button
      v-if="writerResponseCache && cacheEligible"
      class="cache-breakpoint-dot"
      :class="{ manual: !session.cache_breakpoint_auto, planned: cacheBreakpointId === message.id }"
      :disabled="busy || cacheBreakpointBusy || reviewing || switchingVersion || !!editingMessageId"
      title="将缓存断点固定在此回复末尾"
      aria-label="将缓存断点固定在此回复末尾"
      @click="emit('setCacheBreakpoint', message.id)"
    />
    <div v-if="message.role === 'user'" class="avatar">你</div>
    <CharacterPortrait v-else class="avatar" :src="portraitSrc" :name="name" size="message" />
    <div class="message-body">
      <div class="message-meta">
        <strong>{{ message.role === 'user' ? session.persona : name }}</strong>
        <small
          v-if="writerResponseCache && cacheBreakpointId === message.id"
          class="cache-breakpoint-status"
          >{{ session.cache_breakpoint_auto ? '缓存自动跟进' : '缓存位置已固定' }}</small
        >
      </div>
      <InlineMessageEditor
        v-if="editingMessageId === message.id"
        v-model="editingMessageText"
        :original="message.raw"
        :busy="savingEdit"
        :notify-choice="editingNotifyChoice"
        :full-agent="session.agent_mode === 'full_agent'"
        @cancel="emit('cancelEdit')"
        @save="emit('saveEdit', message, $event)"
      />
      <StoryMessage
        v-else
        find-in-dialogue
        :message="message"
        :session="session"
        :enabled="enabled"
        @action="forwardAction"
      />
      <details v-if="message.retrieval_query" class="retrieval-clue">
        <summary>检索线索</summary>
        <p>{{ message.retrieval_query }}</p>
      </details>
      <div v-if="sourceHighlight?.id === message.id && sourceHighlight?.fallback" class="source-excerpt">
        <small>引用原文</small><mark>{{ sourceHighlight?.excerpt }}</mark>
      </div>
      <div v-if="editingMessageId !== message.id" class="message-actions">
        <template v-if="message.role === 'assistant' && message.id === session.head">
          <button
            class="icon-action"
            :disabled="busy || switchingVersion || !!editingMessageId || reviewing || !message.version_index"
            aria-label="上一个版本"
            title="上一个版本（更旧）"
            @click="emit('switchVersion', message, -1)"
          >
            <svg width="14" height="14" viewBox="0 0 20 20" fill="none" aria-hidden="true">
              <path
                d="m12 4-6 6 6 6"
                stroke="currentColor"
                stroke-width="1.4"
                stroke-linecap="round"
                stroke-linejoin="round"
              />
            </svg>
          </button>
          <span class="muted" aria-live="polite"
            >{{ (message.version_index ?? 0) + 1 }} / {{ message.versions?.length || 1 }}</span
          >
          <button
            class="icon-action"
            :disabled="
              busy ||
              switchingVersion ||
              !!editingMessageId ||
              reviewing ||
              (message.version_index ?? 0) >= (message.versions?.length || 1) - 1
            "
            aria-label="下一个版本"
            title="下一个版本（更新）"
            @click="emit('switchVersion', message, 1)"
          >
            <svg width="14" height="14" viewBox="0 0 20 20" fill="none" aria-hidden="true">
              <path
                d="m8 4 6 6-6 6"
                stroke="currentColor"
                stroke-width="1.4"
                stroke-linecap="round"
                stroke-linejoin="round"
              />
            </svg>
          </button>
        </template>
        <EditMessageButton
          v-if="editable"
          :disabled="busy || switchingVersion || !!editingMessageId || reviewing"
          @edit="emit('edit', message, $event)"
        />
        <button
          v-if="message.role === 'assistant'"
          class="icon-action"
          :disabled="busy || !!editingMessageId || reviewing"
          aria-label="分支"
          title="继承此处进度，新建对话"
          @click="emit('fork', message)"
        >
          <svg width="14" height="14" viewBox="0 0 20 20" fill="none" aria-hidden="true">
            <circle cx="5" cy="4" r="2" stroke="currentColor" stroke-width="1.4" />
            <circle cx="5" cy="16" r="2" stroke="currentColor" stroke-width="1.4" />
            <circle cx="15" cy="4" r="2" stroke="currentColor" stroke-width="1.4" />
            <path
              d="M5 6v8m0-4h5a5 5 0 0 0 5-4"
              stroke="currentColor"
              stroke-width="1.4"
              stroke-linecap="round"
            />
          </svg>
        </button>
        <button
          v-if="cacheEligible && writerResponseCache"
          class="icon-action cache-set-action"
          :class="{ active: !session.cache_breakpoint_auto && cacheBreakpointId === message.id }"
          :disabled="busy || cacheBreakpointBusy || !!editingMessageId || reviewing || switchingVersion"
          :aria-label="
            cacheBreakpointId === message.id && !session.cache_breakpoint_auto
              ? '缓存断点已固定在此回复末尾'
              : '将缓存断点固定在此回复末尾'
          "
          :title="
            cacheBreakpointId === message.id && !session.cache_breakpoint_auto
              ? '缓存断点已固定在此回复末尾'
              : '将缓存断点固定在此回复末尾'
          "
          @click="emit('setCacheBreakpoint', message.id)"
        >
          <svg width="16" height="16" viewBox="0 0 20 20" fill="none" aria-hidden="true">
            <path
              d="M4 4v12m0-2h11l-2.5-3L15 8H4"
              stroke="currentColor"
              stroke-width="1.5"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
            <circle cx="4" cy="4" r="2" fill="currentColor" />
          </svg>
        </button>
      </div>
      <div v-if="sentAt" class="message-time">
        <span
          v-if="interval"
          class="message-interval"
          title="距上一条消息"
          :aria-label="`距上一条消息 ${interval}`"
          >{{ interval }}</span
        >
        <time :datetime="sentAt.toISOString()" :title="sentAt.toLocaleString()">{{
          sentAt.toLocaleTimeString('zh-CN', {
            hour12: false,
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
          })
        }}</time>
      </div>
    </div>
  </article>
</template>

<style scoped>
.message-time {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 8px;
  text-align: right;
  color: var(--text-muted);
  font: 11px/1.5 var(--font-ui);
  font-variant-numeric: tabular-nums;
}
</style>
