<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import type { Provider } from '../../lib/settingsDocument';
import { channelProviders, type Channel } from '../../lib/channels';
import ChannelSwitch from '../common/ChannelSwitch.vue';
import { chatgptState, loginPending, subscriptionProvider } from '../../lib/chatgptAccounts';
import ChatGPTConnection from './ChatGPTConnection.vue';
import ChatGPTWelcome from './ChatGPTWelcome.vue';
const props = defineProps<{
  providers: Provider[];
  flush: () => Promise<unknown>;
  initialChannel?: Channel;
}>();
const channel = ref<Channel>(props.initialChannel || 'standard');
const visible = computed(() => channelProviders(props.providers, channel.value));
const emit = defineEmits<{ 'update:providers': [providers: Provider[]]; channel: [channel: Channel] }>();
watch(channel, (value) => emit('channel', value));
function add() {
  emit('update:providers', [
    ...props.providers,
    {
      id: crypto.randomUUID(),
      name: channel.value === 'chatgpt' ? 'OpenAI Official' : '新提供商',
      base_url: 'https://api.openai.com',
      api_key: '',
      use_proxy: true,
      auth_mode: channel.value === 'chatgpt' ? 'chatgpt_subscription' : 'api_key',
      chatgpt_account_id: null,
    },
  ]);
}
</script>
<template>
  <div class="settings-section-heading">
    <p class="muted">按渠道独立管理连接，对话创建后不能跨渠道切换。</p>
  </div>
  <ChannelSwitch v-model="channel" />
  <div class="settings-section-heading">
    <div>
      <h3>{{ channel === 'chatgpt' ? 'ChatGPT 官方' : '普通 API' }}</h3>
      <p class="muted">
        {{
          channel === 'chatgpt'
            ? '使用 ChatGPT 订阅额度，模型与账号权限保持一致。'
            : '使用 API Key 连接提供商，支持各类兼容接口。'
        }}
      </p>
    </div>
    <button @click="add">{{ channel === 'chatgpt' ? '连接 ChatGPT 账号' : '添加提供商' }}</button>
  </div>
  <section
    v-for="provider in visible"
    :key="provider.id"
    class="provider-editor"
    :class="{ 'official-provider': subscriptionProvider(provider) }"
  >
    <details :open="!subscriptionProvider(provider) || !provider.chatgpt_account_id">
      <summary class="provider-summary">
        <span
          ><strong>{{ provider.name || '新提供商' }}</strong
          ><small>{{
            subscriptionProvider(provider) ? '工作区账号 · 订阅连接' : 'API Key 连接'
          }}</small></span
        >
        <span class="provider-summary-status" v-if="subscriptionProvider(provider)">{{
          chatgptState.accounts.find((a) => a.id === provider.chatgpt_account_id)?.status === 'connected'
            ? '已连接'
            : '待连接'
        }}</span>
        <span class="provider-summary-action">{{
          subscriptionProvider(provider) ? '管理账号' : '管理连接'
        }}</span>
      </summary>
      <div class="settings-field-grid">
        <label>名称<input v-model="provider.name" /></label>
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
    </details>
  </section>
  <p v-if="!visible.length" class="muted">此渠道尚未配置连接。</p>
  <p class="channel-provider-note muted">
    {{
      channel === 'chatgpt'
        ? '官方渠道的两种 Agent 模式均支持本地上下文与云端 compact，新对话默认使用本地方式。'
        : '普通渠道的 Full Agent 使用本地换窗。API Key 连接始终属于普通渠道。'
    }}
  </p>
  <ChatGPTWelcome />
</template>
<style scoped>
.provider-summary {
  display: flex;
  align-items: center;
  gap: 20px;
  padding: 18px 0;
  cursor: pointer;
  list-style: none;
}
.provider-summary > span:first-child {
  flex: 1;
  display: grid;
  gap: 7px;
}
.provider-summary strong {
  color: var(--text-primary);
  font-size: 16px;
  font-weight: 500;
}
.provider-summary small {
  color: var(--text-muted);
  font-size: 13px;
}
.provider-summary-status {
  color: var(--success);
  font-size: 13px;
}
.provider-summary-action {
  border: 1px solid var(--border-control);
  border-radius: 6px;
  padding: 9px 14px;
  font-size: 13px;
}
.official-provider {
  border: 1px solid var(--border-control);
  border-radius: 6px;
  padding: 0 20px;
  margin: 18px 0 28px;
}
.official-provider details[open] {
  padding-bottom: 18px;
}
.settings-section-heading h3 {
  margin: 12px 0;
  font-size: 20px;
  font-weight: 500;
}
@media (max-width: 600px) {
  .provider-summary {
    flex-wrap: wrap;
    gap: 10px;
  }
  .provider-summary > span:first-child {
    min-width: 100%;
  }
  .official-provider {
    padding: 0 14px;
  }
}
</style>
