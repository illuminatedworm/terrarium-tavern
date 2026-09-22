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
  });
}
function removeProvider(provider: any) {
  if (props.settings.providers.length <= 1) return;
  const fallback = props.settings.providers.find((item: any) => item.id !== provider.id);
  for (const role of ['foreground', 'background', 'card_editor']) {
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
  return role === 'foreground'
    ? '前台 Writer'
    : role === 'card_editor'
      ? 'Card Editor 编辑 Agent'
      : '后台 Tracker';
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
  <p v-if="!settings.card_editor" class="muted">编辑 Agent 当前沿用后台 Tracker 的模型与预算。</p>
  <section
    v-for="role in ['foreground', 'background', 'card_editor'].filter((r) => settings[r])"
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
        <label
          >温度<input type="number" v-model.number="settings[role].temperature" min="0" max="2" step="0.1"
        /></label>
        <label
          >思考强度<select v-model="settings[role].reasoning_effort">
            <option v-for="e in effortOptions.filter((e) => e.value !== 'inherit')" :value="e.value">
              {{ e.label }}
            </option>
          </select></label
        >
        <label
          >思考参数格式<select v-model="settings[role].reasoning_format">
            <option value="reasoning_effort">reasoning_effort</option>
            <option value="reasoning">reasoning.effort</option>
          </select></label
        >
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
