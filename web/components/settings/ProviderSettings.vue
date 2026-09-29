<script setup lang="ts">
import type { Provider } from '../../lib/settingsDocument';
const props = defineProps<{ providers: Provider[] }>();
const emit = defineEmits<{ 'update:providers': [providers: Provider[]] }>();
function add() {
  emit('update:providers', [
    ...props.providers,
    {
      id: crypto.randomUUID(),
      name: '新提供商',
      base_url: 'https://api.openai.com/v1',
      api_key: '',
      api_mode: 'chat_completions',
      use_proxy: true,
    },
  ]);
}
</script>
<template>
  <div class="settings-section-heading">
    <p class="muted">连接与密钥由两种模式共用。被引用的提供商需先解除引用才能删除。</p>
    <button @click="add">添加提供商</button>
  </div>
  <section v-for="provider in providers" :key="provider.id" class="provider-editor">
    <h4>{{ provider.name || '新提供商' }}</h4>
    <div class="settings-field-grid">
      <label>名称<input v-model="provider.name" /></label>
      <label>接口地址<input v-model="provider.base_url" placeholder="https://…/v1" /></label>
      <label
        >接口方式<select v-model="provider.api_mode" aria-label="接口方式">
          <option value="chat_completions">Chat Completions</option>
          <option value="responses">Responses</option>
          <option value="anthropic">Anthropic Messages</option>
          <option value="gemini">Gemini GenerateContent</option>
        </select></label
      >
      <label
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
    <button
      class="danger-button"
      :disabled="providers.length <= 1"
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
</template>
