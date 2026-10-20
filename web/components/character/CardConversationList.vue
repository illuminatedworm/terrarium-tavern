<script setup lang="ts">
import Icon from '../common/Icon.vue';
import { channelLabels, type Channel } from '../../lib/channels';
import type { CardConversationSummary } from '../../lib/types';

/** Left sidebar list of Card Author conversations. */
defineProps<{ conversations: CardConversationSummary[]; activeId: string; deleting: boolean }>();
const emit = defineEmits<{
  create: [];
  open: [id: string];
  delete: [item: CardConversationSummary, event: MouseEvent];
}>();
const running = (item: CardConversationSummary) => ['queued', 'running'].includes(item.status);
</script>

<template>
  <header class="panel-head">
    <h2>制卡</h2>
    <span class="panel-count">{{ conversations.length }}</span>
  </header>
  <button class="new-story" @click="emit('create')"><Icon name="plus" :size="17" />新制卡对话</button>
  <nav class="session-list" aria-label="制卡对话列表">
    <div
      v-for="conversation in conversations"
      :key="conversation.id"
      class="session-row"
      :data-conversation-id="conversation.id"
      :class="{ active: conversation.id === activeId }"
    >
      <button class="session-open" @click="emit('open', conversation.id)">
        <span class="session-glyph"><Icon name="feather" :size="17" /></span>
        <span
          >{{ conversation.title
          }}<small
            >{{ conversation.reference_count ?? conversation.card_count }} 份资料 ·
            {{ running(conversation) ? '运行中' : conversation.resumable ? '可继续' : '已保存对话' }}
            · {{ channelLabels[conversation.channel_origin as Channel] || '渠道待确认' }}</small
          ></span
        >
      </button>
      <span class="row-actions"
        ><button
          class="session-delete"
          :aria-label="`删除制卡对话 ${conversation.title}`"
          :disabled="deleting || running(conversation) || conversation.resumable"
          :title="
            running(conversation) || conversation.resumable ? '请先停止任务并处理未完成进度' : '删除制卡对话'
          "
          @click.stop="emit('delete', conversation, $event)"
        >
          <Icon name="trash" :size="15" /></button
      ></span>
    </div>
    <p v-if="!conversations.length" class="empty-small">制卡对话开始后，会留在这里。</p>
  </nav>
</template>
