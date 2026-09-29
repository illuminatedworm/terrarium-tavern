<script setup lang="ts">
import { computed, ref } from 'vue';
import { api } from '../../lib/api';
import type { ModelProfile, Provider } from '../../lib/settingsDocument';
import SettingsFields, { type SettingField } from './SettingsFields.vue';
import CacheSettingsEditor from './CacheSettingsEditor.vue';
import { modelEndpoint } from '../../lib/cachePolicy';
const props = defineProps<{
  profile: ModelProfile;
  providers: Provider[];
  role: string;
  label: string;
  effortOptions: Array<{ value: string; label: string }>;
  flush: () => Promise<unknown>;
}>();
const emit = defineEmits<{ checked: [] }>();
const models = ref<string[]>([]);
const busy = ref(false);
const feedback = ref('');
const cacheValid = ref(true);
const provider = computed(() => props.providers.find((item) => item.id === props.profile.provider_id));
const errorPrefix = computed(() =>
  props.role === 'full_agent'
    ? 'full_agent.model.'
    : props.role === 'card_editor'
      ? 'shared.card_editor.'
      : 'half_agent.' + props.role + '.',
);
const common = computed<SettingField[]>(() => [
  { path: 'context_tokens', label: '上下文窗口（tokens）', min: 2048, max: 2000000 },
  { path: 'reply_tokens', label: '输出上限（tokens）', min: 128, max: 65536 },
  {
    path: 'reasoning_effort',
    label: '思考强度',
    options: props.effortOptions.filter((item) => item.value !== 'inherit'),
  },
  { path: 'temperature', label: '温度 temperature', optional: true, min: 0, max: 2, step: 'any' },
  { path: 'top_p', label: '核采样 top_p', optional: true, min: 0, max: 1, step: 'any' },
]);
const advanced = computed<SettingField[]>(() => [
  ...(provider.value?.api_mode !== 'responses'
    ? [{ path: 'top_k', label: '候选数 top_k', optional: true, min: 0 }]
    : []),
  ...(!provider.value || provider.value.api_mode === 'chat_completions'
    ? [
        { path: 'frequency_penalty', label: '频率惩罚', optional: true, min: -2, max: 2, step: 'any' },
        { path: 'presence_penalty', label: '重复惩罚', optional: true, min: -2, max: 2, step: 'any' },
        {
          path: 'reasoning_format',
          label: '思考参数格式',
          options: [
            { value: 'reasoning_effort', label: 'reasoning_effort' },
            { value: 'reasoning', label: 'reasoning.effort' },
          ],
        },
      ]
    : []),
  {
    path: 'tokenizer',
    label: '计数分词器',
    options: [
      { value: 'auto', label: '自动匹配，未知模型参考估算' },
      { value: 'o200k_base', label: 'o200k_base' },
      { value: 'cl100k_base', label: 'cl100k_base' },
    ],
  },
]);
async function action(check: boolean) {
  if (busy.value) return;
  if (!cacheValid.value) {
    feedback.value = '请先修正缓存参数 JSON。';
    return;
  }
  busy.value = true;
  feedback.value = '';
  try {
    await props.flush();
    if (check) {
      const result = await api(`/settings/probe?role=${props.role}`, {});
      feedback.value =
        result.capability === 'accepted'
          ? '连接检查通过，测试上下文已传达。'
          : '连接成功，但未通过测试上下文检查。';
      emit('checked');
    } else {
      models.value = (await api('/settings/models', provider.value)).models;
      feedback.value = `已获取 ${models.value.length} 个模型，可选择或直接输入。`;
    }
  } catch (e) {
    feedback.value = String(e);
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <section class="profile-editor" :aria-label="label">
    <h4 v-if="label">{{ label }}</h4>
    <div class="profile-connection-row">
      <div class="settings-field-grid profile-connection">
        <label
          >提供商<select v-model="profile.provider_id">
            <option v-for="item in providers" :key="item.id" :value="item.id">{{ item.name }}</option>
          </select></label
        >
        <label
          >模型名称<input v-model="profile.model" :list="'models-' + role" /><datalist :id="'models-' + role">
            <option v-for="name in models" :key="name" :value="name" /></datalist
        ></label>
      </div>
      <div class="profile-actions">
        <button class="primary" :disabled="busy" @click="action(false)">获取模型列表</button
        ><button :disabled="busy" @click="action(true)">{{ busy ? '正在检查…' : '检查连接' }}</button>
      </div>
    </div>
    <p v-if="feedback" role="status" class="inline-feedback">{{ feedback }}</p>
    <p v-if="provider" class="muted">
      请求地址：{{ modelEndpoint(provider.base_url, provider.api_mode, profile.model) }}
    </p>
    <h4>常用参数</h4>
    <SettingsFields :value="profile" :fields="common" :error-prefix="errorPrefix"
      ><slot name="common-extra"
    /></SettingsFields>
    <details class="settings-advanced">
      <summary>更多参数：采样细项、分词器与提示缓存</summary>
      <SettingsFields :value="profile" :fields="advanced" :error-prefix="errorPrefix" />
      <CacheSettingsEditor
        v-if="profile.cache"
        :cache="profile.cache"
        :model="profile.model"
        :api="provider?.api_mode || 'chat_completions'"
        @validity="cacheValid = $event"
      />
      <p class="muted">
        采样参数留空即不发送。未知模型采用参考分词器估算，服务商实际用量单独显示。显式缓存断点需要模型支持。
      </p>
    </details>
  </section>
</template>
