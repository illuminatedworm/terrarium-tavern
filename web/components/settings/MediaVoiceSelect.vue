<script setup lang="ts">
import { computed } from 'vue';
import {
  geminiPresetOnly,
  isSeedClone,
  seedPresetId,
  voiceError,
  voiceOptions,
  type SpeechProfile,
  type MediaProtocols,
} from '../../lib/media';
import SelectMenu from '../common/SelectMenu.vue';
const props = defineProps<{
  modelValue: string;
  profile: SpeechProfile;
  catalog: MediaProtocols;
  label: string;
  disabled?: boolean;
}>();
defineEmits<{ 'update:modelValue': [value: string] }>();
const choices = computed(() => voiceOptions(props.profile, props.catalog, props.modelValue));
const problem = computed(() => voiceError(props.profile, props.catalog, props.modelValue));
const presetOnly = computed(() => geminiPresetOnly(props.profile, props.catalog));
const hint = computed(() => {
  if (presetOnly.value) return '当前配置使用旧版 Gemini TTS 预设音色。';
  if (props.profile.adapter === 'gemini_tts') return '可粘贴同一 Google 项目保存的音色 ID（voice_…）。';
  if (isSeedClone(props.profile)) return '粘贴当前语音账号在火山控制台复刻的音色 ID。';
  return '';
});
</script>
<template>
  <div class="voice-select">
    <SelectMenu
      :model-value="modelValue"
      :options="choices"
      :label="label"
      :disabled="disabled"
      :invalid="!!problem"
      searchable
      :allow-custom="(id) => !voiceError(profile, catalog, id)"
      custom-description="使用此音色 ID"
      :search-placeholder="presetOnly ? '搜索预设音色' : '搜索或粘贴音色 ID'"
      :empty-text="presetOnly ? '没有匹配的预设音色' : '请填写提供商保存的音色 ID'"
      @update:model-value="
        $emit(
          'update:modelValue',
          profile.adapter === 'seed_tts' ? (seedPresetId($event, catalog) ?? $event) : $event,
        )
      "
    >
      <template v-if="hint" #context
        ><p class="voice-hint">{{ hint }}</p></template
      >
    </SelectMenu>
    <small v-if="problem" class="voice-error" role="alert">{{ problem }}</small>
  </div>
</template>
<style scoped>
.voice-select {
  min-width: 0;
}
.voice-error {
  display: block;
  color: var(--danger);
  font-size: var(--fs-xs);
}
.voice-hint {
  margin: 0;
  padding: 4px 10px 8px;
  color: var(--text-muted);
  font-size: var(--fs-sm);
}
</style>
