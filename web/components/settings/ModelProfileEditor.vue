<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { api } from '../../lib/api';
import type { ModelProfile, Provider } from '../../lib/settingsDocument';
import SettingsFields, { type SettingField } from './SettingsFields.vue';
import CacheSettingsEditor from './CacheSettingsEditor.vue';
import MessageAdapterEditor from './MessageAdapterEditor.vue';
import { modelEndpoint } from '../../lib/cachePolicy';
import { useModelCatalog } from '../../lib/modelCatalog';
import { modelEffortOptions } from '../../lib/reasoningEffort';
import { useApiReasoning } from '../../lib/apiReasoning';
import { loadChatGPTAccounts, subscriptionProvider } from '../../lib/chatgptAccounts';
import ChatGPTUsage from '../common/ChatGPTUsage.vue';
import { blankChannelProfile, channelProviders, providerChannel, type Channel } from '../../lib/channels';
import ChannelSwitch from '../common/ChannelSwitch.vue';
const props = defineProps<{
  profile: ModelProfile;
  providers: Provider[];
  role: string;
  label: string;
  effortOptions: Array<{ value: string; label: string }>;
  flush: () => Promise<unknown>;
  channel?: Channel;
}>();
const emit = defineEmits<{ checked: [] }>();
const busy = ref(false);
const fetching = ref(false);
const feedback = ref('');
const cacheValid = ref(true);
const provider = computed(() => props.providers.find((item) => item.id === props.profile.provider_id));
const subscription = computed(() => subscriptionProvider(provider.value));
const { details: apiReasoning, note: reasoningNote } = useApiReasoning(
  () => props.profile,
  () => !subscription.value,
);
const editorChannel = ref<Channel>(providerChannel(provider.value));
const visibleProviders = computed(() =>
  props.role === 'card_editor' ? channelProviders(props.providers, editorChannel.value) : props.providers,
);
function switchEditorChannel(channel: Channel) {
  editorChannel.value = channel;
  Object.assign(props.profile, blankChannelProfile(props.profile, props.providers, channel));
}
watch(
  subscription,
  async (enabled) => {
    if (enabled) {
      try {
        await loadChatGPTAccounts();
      } catch (error) {
        feedback.value = String(error);
      }
    }
  },
  { immediate: true },
);
const {
  catalog,
  error: catalogError,
  refresh,
} = useModelCatalog(
  () => props.profile,
  () => provider.value,
  props.flush,
);
const models = computed(() => catalog.value?.models || []);
const defaultPath = computed(() => (props.profile.api_mode === 'gemini' ? 'v1beta' : 'v1'));
watch(
  () => [props.profile.provider_id, props.profile.api_mode, props.profile.api_path],
  () => {
    feedback.value = '';
  },
);
const errorPrefix = computed(() =>
  props.role === 'full_agent'
    ? `full_agent.${props.channel === 'chatgpt' ? 'official_model' : 'model'}.`
    : props.role === 'card_editor'
      ? 'shared.card_editor.'
      : 'half_agent.' + (props.channel === 'chatgpt' ? 'official_' : '') + props.role + '.',
);
const common = computed<SettingField[]>(() => [
  { path: 'context_tokens', label: '上下文窗口（tokens）', min: 2048, max: 2000000 },
  {
    path: 'reply_tokens',
    label: subscription.value ? '输出预留预算（tokens）' : '输出上限（tokens）',
    min: 128,
    max: 65536,
  },
  {
    path: 'reasoning_effort',
    label: '思考强度',
    options: modelEffortOptions(
      props.effortOptions.filter((item) => item.value !== 'inherit'),
      subscription.value
        ? catalog.value?.model_details?.find((item) => item.id === props.profile.model)
        : apiReasoning.value,
      props.profile.reasoning_effort,
      !subscription.value,
    ),
  },
  ...(!subscription.value
    ? [
        { path: 'temperature', label: '温度 temperature', optional: true, min: 0, max: 2, step: 'any' },
        { path: 'top_p', label: '核采样 top_p', optional: true, min: 0, max: 1, step: 'any' },
      ]
    : []),
]);
const advanced = computed<SettingField[]>(() => [
  ...(!subscription.value && props.profile.api_mode !== 'responses'
    ? [{ path: 'top_k', label: '候选数 top_k', optional: true, min: 0 }]
    : []),
  ...(!subscription.value && props.profile.api_mode === 'chat_completions'
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
  fetching.value = !check;
  feedback.value = '';
  try {
    await props.flush();
    if (check) {
      const result = await api(
        `/settings/probe?role=${props.role}${props.channel ? '&channel=' + props.channel : ''}`,
        {},
      );
      feedback.value =
        result.capability === 'accepted'
          ? '连接检查通过，测试上下文已传达。'
          : '连接成功，但未通过测试上下文检查。';
      emit('checked');
    } else {
      const result = await refresh();
      feedback.value = `已更新 ${result.models.length} 个模型，列表已缓存。`;
    }
  } catch (e) {
    feedback.value = String(e);
  } finally {
    busy.value = false;
    fetching.value = false;
  }
}
</script>
<template>
  <section class="profile-editor" :aria-label="label">
    <ChannelSwitch
      v-if="role === 'card_editor'"
      :model-value="editorChannel"
      @update:model-value="switchEditorChannel"
    />
    <h4 v-if="label">{{ label }}</h4>
    <div class="profile-connection-row">
      <div class="settings-field-grid profile-connection">
        <label
          >提供商<select v-model="profile.provider_id" aria-label="提供商" :disabled="busy">
            <option v-for="item in visibleProviders" :key="item.id" :value="item.id">{{ item.name }}</option>
          </select></label
        >
        <label v-if="!subscription"
          >接口方式<select v-model="profile.api_mode" aria-label="接口方式" :disabled="busy">
            <option value="chat_completions">Chat Completions</option>
            <option value="responses">Responses</option>
            <option value="anthropic">Anthropic Messages</option>
            <option value="gemini">Gemini GenerateContent</option>
          </select></label
        >
        <label v-if="!subscription"
          >API 路径<input v-model="profile.api_path" :placeholder="`默认：${defaultPath}`" :disabled="busy"
        /></label>
        <p v-else class="muted">ChatGPT 订阅 · Responses</p>
        <label
          >模型名称<input v-model="profile.model" aria-label="模型名称" :list="'models-' + role" /><datalist
            :id="'models-' + role"
          >
            <option v-for="name in models" :key="name" :value="name">
              {{ catalog?.model_details?.find((item) => item.id === name)?.display_name || name }}
            </option>
          </datalist></label
        >
      </div>
      <div class="profile-actions">
        <button class="primary" :disabled="busy" @click="action(false)">
          {{ fetching ? '正在更新…' : '更新模型列表' }}</button
        ><button :disabled="busy" @click="action(true)">
          {{ busy && !fetching ? '正在检查…' : '检查连接' }}
        </button>
      </div>
    </div>
    <p v-if="feedback" role="status" class="inline-feedback">{{ feedback }}</p>
    <p v-else-if="catalogError.message" class="inline-feedback">{{ catalogError.message }}</p>
    <p class="muted">
      {{
        catalog?.updated_at
          ? `已缓存 ${models.length} 个模型 · 更新于 ${new Date(catalog.updated_at * 1000).toLocaleString()}`
          : '暂无模型列表缓存，可更新列表或直接输入模型名称。'
      }}
    </p>
    <p v-if="provider" class="muted">
      请求地址：{{
        subscription
          ? 'https://api.openai.com/v1/responses'
          : modelEndpoint(provider.base_url, profile.api_mode, profile.model, profile.api_path)
      }}
    </p>
    <ChatGPTUsage :active="subscription" />
    <h4>常用参数</h4>
    <SettingsFields :value="profile" :fields="common" :error-prefix="errorPrefix"
      ><slot name="common-extra"
    /></SettingsFields>
    <p v-if="!subscription" class="muted">{{ reasoningNote }}</p>
    <p v-if="subscription" class="muted">
      输出预留预算用于本地上下文计算，不是服务端输出上限。ChatGPT
      订阅通道不支持温度、采样及显式缓存控制；普通渠道的模型配置独立保存。
    </p>
    <details class="settings-advanced">
      <summary>更多参数：采样细项、分词器与提示缓存</summary>
      <SettingsFields :value="profile" :fields="advanced" :error-prefix="errorPrefix" />
      <CacheSettingsEditor
        v-if="profile.cache && !subscription"
        :cache="profile.cache"
        :model="profile.model"
        :api="profile.api_mode"
        @validity="cacheValid = $event"
      />
      <p v-if="!subscription" class="muted">
        采样参数留空即不发送。未知模型采用参考分词器估算，服务商实际用量单独显示。显式缓存断点需要模型支持。
      </p>
    </details>
    <details class="settings-advanced">
      <summary>消息适配：发送角色与标签包裹</summary>
      <p class="muted">这里只影响新建的游玩或制卡对话。已有对话在自己的消息适配设置中修改。</p>
      <MessageAdapterEditor
        :model-value="profile.message_adapter"
        :api-mode="subscription ? 'responses' : profile.api_mode"
        :lane="role"
        @update:model-value="profile.message_adapter = $event"
      />
    </details>
  </section>
</template>
