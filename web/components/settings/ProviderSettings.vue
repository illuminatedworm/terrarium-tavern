<script setup lang="ts">
import type { Provider } from '../../lib/settingsDocument';
import { loginPending, subscriptionProvider } from '../../lib/chatgptAccounts';
import ChatGPTConnection from './ChatGPTConnection.vue';
import ChatGPTWelcome from './ChatGPTWelcome.vue';
const props = defineProps<{ providers: Provider[]; flush: () => Promise<unknown> }>();
const emit = defineEmits<{ 'update:providers': [providers: Provider[]] }>();
function add() {
  emit('update:providers', [
    ...props.providers,
    {
      id: crypto.randomUUID(),
      name: '新提供商',
      base_url: 'https://api.openai.com',
      api_key: '',
      use_proxy: true,
      auth_mode: 'api_key',
      chatgpt_account_id: null,
    },
  ]);
}
</script>
<template>
  <div class="settings-section-heading">
    <p class="muted">
      提供商由各 Agent 共用。可配置 API Key 或连接 ChatGPT 订阅；各 Agent 分别选择提供商与模型。
    </p>
    <button @click="add">添加提供商</button>
  </div>
  <section v-for="provider in providers" :key="provider.id" class="provider-editor">
    <h4>{{ provider.name || '新提供商' }}</h4>
    <div class="settings-field-grid">
      <label>名称<input v-model="provider.name" /></label>
      <label
        >认证方式<select
          v-model="provider.auth_mode"
          aria-label="认证方式"
          :disabled="loginPending(provider.id)"
        >
          <option value="api_key">API Key</option>
          <option value="chatgpt_subscription">ChatGPT 订阅</option>
        </select></label
      >
      <label v-if="!subscriptionProvider(provider)"
        >主域名<input v-model="provider.base_url" placeholder="https://api.example.com"
      /></label>
      <label v-if="!subscriptionProvider(provider)"
        >接口密钥<input
          v-model="provider.api_key"
          type="password"
          autocomplete="off"
          :placeholder="provider.has_api_key ? '已保存，留空保留' : '输入密钥'"
      /></label>
      <label class="settings-check"
        ><span>使用接口代理</span><input v-model="provider.use_proxy" type="checkbox"
      /></label>
    </div>
    <ChatGPTConnection v-if="subscriptionProvider(provider)" :provider="provider" :flush="flush" />
    <button
      class="danger-button"
      :disabled="providers.length <= 1 || loginPending(provider.id)"
      @click="
        emit(
          'update:providers',
          providers.filter((p) => p.id !== provider.id),
        )
      "
    >
      删除提供商
    </button>
  </section>
  <ChatGPTWelcome />
</template>
