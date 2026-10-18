<script setup lang="ts">
import type { CardAuthorConfig, ModelProfile, Provider, WebSettings } from '../../lib/settingsDocument';
import ModelProfileEditor from './ModelProfileEditor.vue';
import WebModeSettings from './WebModeSettings.vue';
defineProps<{
  value: CardAuthorConfig;
  profile: ModelProfile;
  providers: Provider[];
  services: WebSettings;
  effortOptions: Array<{ value: string; label: string }>;
  flush: () => Promise<unknown>;
}>();
defineEmits<{ checked: []; services: [] }>();
</script>
<template>
  <div class="agent-settings card-author-settings">
    <ModelProfileEditor
      :profile="profile"
      :providers="providers"
      role="card_author"
      label="制卡 agent 模型"
      :effort-options="effortOptions"
      :flush="flush"
      @checked="$emit('checked')"
    />
    <fieldset>
      <legend>创作与上下文</legend>
      <div class="agent-setting-grid">
        <label
          >上下文提醒比例<input
            v-model.number="value.pressure_ratio"
            type="number"
            min="0.25"
            max="0.9"
            step="0.05"
        /></label>
        <label
          >创作进度上限（tokens）<input
            v-model.number="value.handoff_tokens"
            type="number"
            min="128"
            max="16384"
        /></label>
      </div>
      <label
        >每次发送或继续的模型请求上限<input v-model.number="value.max_steps" type="number" min="2" max="100"
      /></label>
    </fieldset>
    <WebModeSettings :value="value.web" :services="services" @services="$emit('services')" />
    <fieldset>
      <legend>制卡 agent 系统提示词</legend>
      <p class="muted">保存后从下一次发送或继续生效，工具权限与草稿审核仍由程序控制。</p>
      <slot />
    </fieldset>
  </div>
</template>
