<script setup lang="ts">
import StoryProse from '../story/StoryProse.vue';
import CardWorkspaceIcon from './CardWorkspaceIcon.vue';
defineProps<{ text: string; role?: string; status?: 'intermediate' | 'streaming' | 'interrupted' }>();
</script>
<template>
  <article
    class="cw-message"
    :class="role || 'assistant'"
    :aria-live="status === 'streaming' ? 'polite' : undefined"
  >
    <span class="cw-avatar" aria-hidden="true" :class="{ 'cw-avatar-user': role === 'user' }">
      <template v-if="role === 'user'">你</template><CardWorkspaceIcon v-else name="pen" />
    </span>
    <div class="cw-message-body">
      <div class="cw-message-meta">
        <strong>{{ role === 'user' ? '你' : '角色卡编辑 agent' }}</strong>
        <small v-if="status" :class="{ 'cw-working': status === 'streaming' }">{{
          status === 'streaming' ? '处理中' : status === 'interrupted' ? '未完成' : '中间回复'
        }}</small>
      </div>
      <div class="cw-message-content"><StoryProse :text="text" :breaks="role === 'user'" /></div>
    </div>
  </article>
</template>
<style scoped>
.cw-avatar {
  display: inline-flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
  width: 35px;
  height: 35px;
  border-radius: 11px;
  background: var(--accent-soft);
  color: var(--accent-hover);
}
.cw-avatar-user {
  background: var(--surface-active);
  color: var(--text-secondary);
  font: 500 15px var(--font-ui);
}
.cw-message {
  display: flex;
  gap: 17px;
  margin: 0 0 28px;
  min-width: 0;
  overflow-wrap: anywhere;
}
.cw-message-body {
  min-width: 0;
  flex: 1;
}
.cw-message-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 4px 0 12px;
  color: var(--text-secondary);
}
.cw-message-meta strong {
  font: 500 13px/1.5 var(--font-ui);
  color: var(--text-primary);
}
.cw-message-meta small {
  color: var(--text-muted);
  font-size: 11px;
}
.cw-message-content :deep(.prose) {
  font-family: var(--story-font-family, var(--font-ui));
  font-size: var(--story-font-size, 16px);
  line-height: var(--story-line-height, 1.9);
}
.cw-message-content :deep(.prose > :first-child) {
  margin-top: 0;
}
.cw-message-content :deep(.prose > :last-child) {
  margin-bottom: 0;
}
.cw-message-content :deep(pre) {
  max-width: 100%;
  overflow-x: auto;
}
.cw-message.user :deep(.prose) {
  color: var(--text-primary);
}
.cw-message-meta .cw-working {
  color: var(--accent);
}
.cw-working::before {
  content: '';
  display: inline-block;
  width: 5px;
  height: 5px;
  margin-right: 7px;
  border-radius: 50%;
  background: currentColor;
  vertical-align: middle;
}
@media (max-width: 620px) {
  .cw-message {
    gap: 10px;
    margin-bottom: 24px;
  }
  .cw-avatar {
    width: 29px;
    height: 29px;
    border-radius: 9px;
  }
  .cw-message-meta {
    margin-top: 2px;
    margin-bottom: 9px;
  }
}
</style>
