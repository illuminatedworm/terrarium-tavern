<script setup lang="ts">
import { computed } from 'vue';
import { voiceOptions, type SpeechProfile, type MediaProtocols } from '../../lib/media';
import SelectMenu from '../common/SelectMenu.vue';
const props = defineProps<{
  modelValue: string;
  profile: SpeechProfile;
  catalog: MediaProtocols;
  label: string;
  disabled?: boolean;
}>();
defineEmits<{ 'update:modelValue': [value: string] }>();
const choices = computed(() => voiceOptions(props.profile, props.catalog));
const custom = computed(() => ['openai_speech', 'seed_tts'].includes(props.profile.adapter));
</script>
<template>
  <SelectMenu
    :model-value="modelValue"
    :options="choices"
    :label="label"
    :disabled="disabled"
    searchable
    :allow-custom="custom"
    custom-description="使用此音色 ID"
    :search-placeholder="custom ? '搜索或输入音色 ID' : '搜索音色'"
    @update:model-value="
      $emit(
        'update:modelValue',
        profile.adapter === 'seed_tts'
          ? (choices.find((option) => option.label === $event)?.value ?? $event)
          : $event,
      )
    "
  />
</template>
