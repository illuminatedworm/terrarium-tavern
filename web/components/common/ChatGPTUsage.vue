<script setup lang="ts">
import { CHATGPT_USAGE_URL } from '../../lib/chatgptAccounts';
defineProps<{ active?: boolean; label?: string; error?: string }>();
</script>
<template>
  <span
    v-if="active"
    class="chatgpt-usage"
    :class="{ 'chatgpt-limit': error?.includes('subscription_sharing_usage_limit_exceeded') }"
  >
    <span>{{
      error?.includes('subscription_sharing_usage_limit_exceeded')
        ? 'ChatGPT 用量已达限制'
        : label || '使用 ChatGPT 订阅'
    }}</span>
    <a :href="CHATGPT_USAGE_URL" target="_blank" rel="noopener noreferrer">管理用量</a>
  </span>
</template>
<style scoped>
.chatgpt-usage {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.6;
}
.chatgpt-usage a {
  color: var(--text-secondary);
  text-decoration: underline;
  text-underline-offset: 3px;
}
.chatgpt-limit a {
  padding: 3px 8px;
  border: 1px solid var(--border-subtle);
  border-radius: 5px;
  color: var(--text-primary);
}
</style>
