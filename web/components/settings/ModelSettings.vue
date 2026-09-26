<script setup lang="ts">
import { ref } from 'vue';
import { api } from '../../lib/api';
const props = defineProps<{ settings: any; effortOptions: Array<{ value: string; label: string }> }>();
const result = ref('');
const models = ref<Record<string, string[]>>({}),
  loading = ref<Record<string, boolean>>({});
async function fetchModels(provider: any) {
  loading.value[provider.id] = true;
  result.value = '';
  try {
    models.value[provider.id] = (await api('/settings/models', provider)).models;
    result.value = `已获取 ${models.value[provider.id].length} 个模型，可在下方选择或手动输入。`;
  } catch (e: any) {
    result.value = e.message;
  } finally {
    loading.value[provider.id] = false;
  }
}
function add() {
  props.settings.providers.push({
    id: crypto.randomUUID(),
    name: '新提供商',
    base_url: '',
    api_key: '',
    use_proxy: true,
    api_mode: 'chat_completions',
  });
}
function setOptionalNumber(role: string, field: string, event: Event) {
  const value = (event.target as HTMLInputElement).value;
  props.settings[role][field] = value === '' ? null : Number(value);
}
function removeProvider(provider: any) {
  if (props.settings.providers.length <= 1) return;
  const fallback = props.settings.providers.find((item: any) => item.id !== provider.id);
  for (const role of ['foreground', 'background', 'card_editor', 'full_agent']) {
    if (props.settings[role]?.provider_id === provider.id) {
      props.settings[role].provider_id = fallback.id;
    }
  }
  props.settings.providers = props.settings.providers.filter((item: any) => item.id !== provider.id);
  delete models.value[provider.id];
  delete loading.value[provider.id];
  result.value = `已移除 ${provider.name || '提供商'}；原先使用它的模型配置已切换到 ${fallback.name || '剩余提供商'}。`;
}
function roleLabel(role: string) {
  if (role === 'full_agent') return 'Full Agent';
  return role === 'foreground'
    ? 'Half Agent · 前台 Writer'
    : role === 'card_editor'
      ? 'Card Editor 编辑 Agent'
      : 'Half Agent · 后台 Tracker';
}
function roleMode(role: string) {
  return (
    props.settings.providers.find((p: any) => p.id === props.settings[role].provider_id)?.api_mode ||
    'chat_completions'
  );
}
async function probe(role: string) {
  try {
    result.value = JSON.stringify(await api(`/settings/probe?role=${role}`, {}), null, 2);
  } catch (e) {
    result.value = String(e);
  }
}
</script>
<template>
  <div class="model-settings-heading">
    <h3>API 提供商</h3>
    <button type="button" @click="add">添加提供商</button>
  </div>
  <div class="provider-list">
    <details v-for="provider in settings.providers" :key="provider.id" class="provider-settings">
      <summary>
        <span>{{ provider.name || '新提供商' }}</span>
        <small>{{ provider.base_url || '尚未设置接口地址' }}</small>
      </summary>
      <div class="provider-fields">
        <label>名称<input v-model="provider.name" /></label>
        <label>接口地址<input v-model="provider.base_url" placeholder="https://…/v1" /></label>
        <label
          >API 方式<select v-model="provider.api_mode">
            <option value="chat_completions">Chat Completions</option>
            <option value="responses">Responses</option>
          </select></label
        >
        <label
          >API Key<input
            v-model="provider.api_key"
            type="password"
            autocomplete="off"
            :placeholder="provider.has_api_key ? '已保存，留空保留' : '输入密钥'"
        /></label>
        <label><input type="checkbox" v-model="provider.use_proxy" />使用 API 代理</label>
        <div class="provider-actions">
          <button
            type="button"
            class="danger-button"
            :disabled="settings.providers.length <= 1"
            :title="settings.providers.length <= 1 ? '至少保留一个 API 提供商' : ''"
            @click="removeProvider(provider)"
          >
            删除提供商
          </button>
          <small v-if="settings.providers.length <= 1">至少保留一个提供商</small>
        </div>
      </div>
    </details>
  </div>
  <label
    ><input
      type="checkbox"
      :checked="!!settings.card_editor"
      @change="
        settings.card_editor = ($event.target as HTMLInputElement).checked
          ? JSON.parse(JSON.stringify(settings.background))
          : null
      "
    />
    为角色卡编辑 Agent 单独配置模型</label
  >
  <p v-if="!settings.card_editor" class="muted">
    编辑 Agent 当前沿用后台 Tracker 的模型与全部参数；启用独立配置后可分别调整。
  </p>
  <label
    ><input
      type="checkbox"
      :checked="!!settings.full_agent"
      @change="
        settings.full_agent = ($event.target as HTMLInputElement).checked
          ? JSON.parse(JSON.stringify(settings.foreground))
          : null
      "
    />
    为 Full Agent 单独配置模型</label
  >
  <p v-if="!settings.full_agent" class="muted">
    Full Agent 沿用 Writer 的连接与预算配置，使用独立的系统提示词和工具循环。
  </p>
  <section
    v-for="role in ['foreground', 'background', 'card_editor', 'full_agent'].filter((r) => settings[r])"
    :key="role"
    class="model-profile"
  >
    <h3>{{ roleLabel(role) }}</h3>
    <label
      >提供商<select v-model="settings[role].provider_id">
        <option v-for="p in settings.providers" :key="p.id" :value="p.id">{{ p.name }}</option>
      </select></label
    >
    <label class="model-name-field">
      模型名称
      <span class="model-input-row">
        <input v-model="settings[role].model" :list="`models-${role}`" />
        <button
          type="button"
          :disabled="loading[settings[role].provider_id]"
          @click="fetchModels(settings.providers.find((p: any) => p.id === settings[role].provider_id))"
        >
          {{ loading[settings[role].provider_id] ? '正在获取…' : '获取模型列表' }}
        </button>
      </span>
      <datalist :id="`models-${role}`">
        <option v-for="model in models[settings[role].provider_id] || []" :key="model" :value="model" />
      </datalist>
    </label>
    <details class="model-advanced">
      <summary>高级参数</summary>
      <div class="advanced-fields">
        <div class="form-row">
          <label
            >上下文窗口（tokens）<input
              type="number"
              v-model.number="settings[role].context_tokens"
              min="2048" /></label
          ><label
            >输出上限（tokens）<input type="number" v-model.number="settings[role].reply_tokens" min="128"
          /></label>
        </div>
        <label
          >计数分词器<select v-model="settings[role].tokenizer">
            <option value="auto">自动匹配；未知模型采用 o200k_base 参考估算</option>
            <option value="o200k_base">o200k_base</option>
            <option value="cl100k_base">cl100k_base</option>
          </select></label
        >
        <p class="muted">
          预算单位为 tokens，本地使用 BPE
          分词器计数，消息封装开销为估算。无法自动匹配的模型采用参考分词器；服务商实际用量单独显示。
        </p>
        <p class="muted">以下采样参数留空即不发送。模型不支持已填写的参数时，将显示接口原错误。</p>
        <div class="form-row optional-sampling">
          <label
            >温度 temperature<input
              type="number"
              :value="settings[role].temperature ?? ''"
              min="0"
              max="2"
              step="any"
              @input="setOptionalNumber(role, 'temperature', $event)"
          /></label>
          <label
            >top_p<input
              type="number"
              :value="settings[role].top_p ?? ''"
              min="0"
              max="1"
              step="any"
              @input="setOptionalNumber(role, 'top_p', $event)"
          /></label>
          <label v-if="roleMode(role) === 'chat_completions'"
            >top_k<input
              type="number"
              :value="settings[role].top_k ?? ''"
              min="0"
              step="1"
              @input="setOptionalNumber(role, 'top_k', $event)"
          /></label>
          <label v-if="roleMode(role) === 'chat_completions'"
            >frequency_penalty<input
              type="number"
              :value="settings[role].frequency_penalty ?? ''"
              min="-2"
              max="2"
              step="any"
              @input="setOptionalNumber(role, 'frequency_penalty', $event)"
          /></label>
          <label v-if="roleMode(role) === 'chat_completions'"
            >presence_penalty<input
              type="number"
              :value="settings[role].presence_penalty ?? ''"
              min="-2"
              max="2"
              step="any"
              @input="setOptionalNumber(role, 'presence_penalty', $event)"
          /></label>
        </div>
        <p v-if="roleMode(role) === 'chat_completions'" class="muted">
          top_k 是兼容服务扩展参数，OpenAI 官方 Chat Completions 参数表未列出。
        </p>
        <div class="cache-settings">
          <h4>提示缓存</h4>
          <label
            >缓存方式<select v-model="settings[role].prompt_cache_mode">
              <option value="default">接口默认</option>
              <option value="no_write">关闭缓存读写（GPT-5.6+）</option>
              <option value="instructions">仅在开头指令末尾放断点（GPT-5.6+）</option>
            </select></label
          >
          <label
            >缓存键（可留空）<input
              v-model.trim="settings[role].prompt_cache_key"
              maxlength="256"
              placeholder="相同前缀使用稳定的键"
          /></label>
          <label
            >旧模型缓存保留时长<select v-model="settings[role].prompt_cache_retention">
              <option value="default">接口默认</option>
              <option value="in_memory">in_memory</option>
              <option value="24h">24h</option>
            </select></label
          >
          <p class="muted">
            后两项使用 explicit
            模式：关闭缓存时不放断点；指令断点放在开头连续系统指令的最后一条，后续内容不在该断点写入。
            仅适用于支持显式断点的模型，且前缀达到最小长度才会被缓存。缓存键留空时，游玩 Agent
            沿用运行时会话键。
          </p>
        </div>
        <label
          >思考强度<select v-model="settings[role].reasoning_effort">
            <option v-for="e in effortOptions.filter((e) => e.value !== 'inherit')" :value="e.value">
              {{ e.label }}
            </option>
          </select></label
        >
        <label v-if="roleMode(role) === 'chat_completions'"
          >思考参数格式<select v-model="settings[role].reasoning_format">
            <option value="reasoning_effort">reasoning_effort</option>
            <option value="reasoning">reasoning.effort</option>
          </select></label
        >
        <p v-else class="muted">Responses 使用 reasoning.effort 参数。</p>
        <button type="button" @click="probe(role)">检查已保存的{{ roleLabel(role) }}模型</button>
      </div>
    </details>
  </section>
  <pre v-if="result">{{ result }}</pre>
</template>

<style scoped>
.model-settings-heading,
.provider-actions,
.model-input-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.model-settings-heading {
  justify-content: space-between;
  margin-bottom: 10px;
}
.model-settings-heading h3 {
  margin: 0;
}
.model-settings-heading button {
  padding: 7px 11px;
}
.provider-list {
  display: grid;
  gap: 8px;
}
.provider-settings,
.model-profile,
.model-advanced {
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  background: color-mix(in srgb, var(--surface-panel) 55%, transparent);
}
.provider-settings > summary,
.model-advanced > summary {
  cursor: pointer;
  padding: 12px 14px;
  color: var(--text-secondary);
}
.provider-settings > summary span {
  color: var(--text-primary);
}
.provider-settings > summary small {
  margin-left: 10px;
}
.provider-fields,
.advanced-fields {
  padding: 0 14px 14px;
}
.provider-actions {
  margin-top: 14px;
}
.danger-button {
  color: var(--danger);
}
.model-profile {
  margin-top: 14px;
  padding: 16px;
}
.model-profile h3 {
  margin-bottom: 0;
}
.model-input-row input {
  flex: 1;
  min-width: 0;
}
.model-input-row button {
  flex: none;
  margin-top: 8px;
  white-space: nowrap;
}
.model-advanced {
  margin-top: 16px;
  background: transparent;
}
.advanced-fields > label:first-child,
.advanced-fields .form-row > label {
  margin-top: 10px;
}
.optional-sampling {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 8px 16px;
}
.cache-settings {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--border-subtle);
}
.cache-settings h4 {
  margin: 0 0 8px;
}
@media (max-width: 620px) {
  .provider-settings > summary small {
    display: block;
    overflow: hidden;
    margin: 4px 0 0;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .model-input-row {
    align-items: stretch;
    flex-direction: column;
  }
  .model-input-row button {
    align-self: flex-start;
    margin-top: 0;
  }
}
</style>
