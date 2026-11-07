<script setup lang="ts">
import { computed } from 'vue';
import { CHATGPT_USAGE_URL } from '../../lib/chatgptAccounts';
import type { Provider } from '../../lib/settingsDocument';
const props = defineProps<{
  active?: boolean;
  label?: string;
  error?: string;
  authMode?: Provider['auth_mode'];
}>();
const officialApi = computed(() => props.authMode === 'openai_api_key');
const subscriptionLimit = computed(
  () => !officialApi.value && props.error?.includes('subscription_sharing_usage_limit_exceeded'),
);
</script>
<template>
  <span v-if="active" class="chatgpt-usage" :class="{ 'chatgpt-limit': subscriptionLimit }">
    <span>{{
      subscriptionLimit
        ? 'ChatGPT 用量已达限制'
        : label || (officialApi ? 'OpenAI 官方 API · API 计费' : '使用 ChatGPT 订阅')
    }}</span>
    <a
      :href="officialApi ? 'https://platform.openai.com/usage' : CHATGPT_USAGE_URL"
      target="_blank"
      rel="noopener noreferrer"
      >管理用量</a
    >
  </span>
</template>
<style scoped>
.chatgpt-usage {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  color: var(--text-muted);
  font-size: var(--fs-sm);
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
  border-radius: var(--radius-xs);
  color: var(--text-primary);
}
</style>
