<script setup lang="ts">
import { computed, ref } from 'vue';
import type { Provider, SettingsDocument, WebSettings } from '../../lib/settingsDocument';
import { blankChannelProfile, channelProviders, type Channel } from '../../lib/channels';
import ChannelSwitch from '../common/ChannelSwitch.vue';
import ModelProfileEditor from './ModelProfileEditor.vue';
import WebModeSettings from './WebModeSettings.vue';
const props = defineProps<{
  value: SettingsDocument['shared']['advisor'];
  services: WebSettings;
  providers: Provider[];
  initialChannel?: Channel;
  effortOptions: Array<{ value: string; label: string }>;
  flush: () => Promise<unknown>;
}>();
defineEmits<{ checked: []; services: [] }>();
const channel = ref<Channel>(props.initialChannel || 'standard');
const profile = computed(() =>
  channel.value === 'chatgpt' ? props.value.official_model : props.value.model,
);
const providers = computed(() => channelProviders(props.providers, channel.value));
function create() {
  props.value.official_model = blankChannelProfile(props.value.model, props.providers, 'chatgpt');
}
</script>
<template>
  <div class="advisor-settings">
    <ChannelSwitch v-model="channel" />
    <p class="muted">参谋 agent 与故事使用同一渠道，供应商和模型可以独立选择。</p>
    <ModelProfileEditor
      v-if="profile"
      :key="channel"
      :profile="profile"
      :providers="providers"
      role="advisor"
      :channel="channel"
      label="参谋 agent 模型"
      :effort-options="effortOptions"
      :flush="flush"
      @checked="$emit('checked')"
    />
    <div v-else class="channel-empty">
      <p>尚未配置官方渠道的参谋 agent 模型。</p>
      <button :disabled="!providers.length" @click="create">配置此渠道</button>
    </div>
    <fieldset>
      <legend>故事与咨询上下文</legend>
      <div class="advisor-setting-grid">
        <label
          >默认带入近期交流（轮）<input
            v-model.number="value.recent_exchanges"
            type="number"
            min="0"
            max="1000"
        /></label>
        <label
          >整理后保留近期咨询（轮）<input v-model.number="value.keep_recent" type="number" min="1" max="100"
        /></label>
      </div>
      <p class="muted">按完整交流选取，0 关闭剧情自动注入。故事内可以覆盖默认轮数；选中的原文不会截断。</p>
      <label class="settings-check"
        ><span>超过参谋 agent 上下文容量时自动整理旧咨询</span
        ><input v-model="value.auto_compact" type="checkbox"
      /></label>
      <label
        >官方渠道默认上下文方式<select v-model="value.context_strategy">
          <option value="local">本地咨询摘要</option>
          <option value="openai_compact">云端 compact</option>
        </select></label
      >
      <p class="muted">
        普通 API 使用本地咨询摘要。参谋 agent 摘要只保存讨论进展，不修改故事的 handoff、note 或 plot。
      </p>
      <label
        >每次发送或继续的模型请求上限<input v-model.number="value.max_steps" type="number" min="2" max="100"
      /></label>
    </fieldset>
    <WebModeSettings :value="value.web" :services="services" @services="$emit('services')" />
    <fieldset>
      <legend>参谋 agent 系统提示词</legend>
      <slot />
    </fieldset>
  </div>
</template>
<style scoped>
.advisor-settings {
  display: grid;
  gap: 14px;
}
.advisor-settings fieldset {
  min-width: 0;
  padding: 18px;
  margin: 0;
  border: 1px solid var(--border-subtle);
  border-radius: 10px;
  display: grid;
  gap: 12px;
}
.advisor-settings legend {
  padding: 0 6px;
  font-size: 13px;
  color: var(--text-secondary);
}
.advisor-settings label {
  display: grid;
  gap: 6px;
  font-size: 13px;
}
.advisor-setting-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.advisor-settings .settings-check {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.advisor-settings .muted {
  margin: 0;
  font-size: 12px;
  line-height: 1.7;
}
@media (max-width: 640px) {
  .advisor-setting-grid {
    grid-template-columns: 1fr;
  }
}
</style>
