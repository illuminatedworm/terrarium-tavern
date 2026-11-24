<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import {
  modelKey,
  sameModel,
  voiceError,
  type ModelKey,
  type SpeechProfile,
  type VoiceTable,
  type MediaProtocols,
} from '../../lib/media';
import type { Provider } from '../../lib/settingsDocument';
import MediaVoiceSelect from '../settings/MediaVoiceSelect.vue';
const props = defineProps<{
  tables: VoiceTable[];
  story: ModelKey;
  storyProviderName: string;
  speech: SpeechProfile;
  catalog: MediaProtocols;
  providers: Provider[];
  disabled: boolean;
}>();
const emit = defineEmits<{ change: [value: VoiceTable[]] }>();
const table = computed(() =>
  props.tables.find((t) => sameModel(t.story_model, props.story) && sameModel(t.speech_model, props.speech)),
);
const name = ref(''),
  voice = ref('');
watch(
  [
    () => props.story.provider_id,
    () => props.story.model,
    () => props.speech.provider_id,
    () => props.speech.model,
    () => props.speech.default_voice,
  ],
  () => {
    name.value = '';
    voice.value = props.speech.default_voice;
  },
  { immediate: true },
);
function describe(key: ModelKey, providerName?: string) {
  return (
    [
      providerName ||
        props.providers.find((p) => p.id === key.provider_id)?.name ||
        (key.provider_id ? '已移除的提供商' : ''),
      key.model,
    ]
      .filter(Boolean)
      .join(' · ') || '未配置'
  );
}
function change(speaker: string, id?: string) {
  const voices = { ...table.value?.voices, ...(id ? { [speaker]: id } : {}) };
  if (!id) delete voices[speaker];
  const others = props.tables.filter((t) => t !== table.value);
  if (Object.keys(voices).length)
    others.push({ story_model: modelKey(props.story), speech_model: modelKey(props.speech), voices });
  emit('change', others);
}
function add() {
  change(name.value.trim(), voice.value);
  name.value = '';
}
</script>
<template>
  <div class="role-voices">
    <dl>
      <dt>剧情模型</dt>
      <dd>{{ describe(story, storyProviderName) }}</dd>
      <dt>配音模型</dt>
      <dd>{{ describe(speech) }}</dd>
    </dl>
    <small>新角色首次提交配音时绑定默认音色 {{ speech.default_voice }}。切回这组模型会恢复下表。</small>
    <div v-for="(id, speaker) in table?.voices" :key="speaker" class="role-voice">
      <strong>{{ speaker }}</strong>
      <MediaVoiceSelect
        :model-value="id"
        :label="speaker + ' 的音色'"
        :profile="speech"
        :catalog="catalog"
        :disabled="disabled"
        @update:model-value="change(speaker, $event)"
      />
      <button
        type="button"
        class="quiet"
        :disabled="disabled"
        :aria-label="'移除 ' + speaker + ' 的绑定'"
        @click="change(speaker)"
      >
        移除
      </button>
    </div>
    <div class="role-add">
      <label class="role-add-field">
        <span>角色名</span>
        <input v-model="name" placeholder="角色或旁白" :disabled="disabled" />
      </label>
      <div class="role-add-field">
        <span>音色</span>
        <MediaVoiceSelect
          v-model="voice"
          label="新增角色的音色"
          :profile="speech"
          :catalog="catalog"
          :disabled="disabled"
        />
      </div>
      <button
        type="button"
        :disabled="
          disabled ||
          !name.trim() ||
          !story.model ||
          !!voiceError(speech, catalog, voice) ||
          !speech.provider_id ||
          !speech.model ||
          Object.hasOwn(table?.voices || {}, name.trim())
        "
        @click="add"
      >
        添加角色
      </button>
    </div>
  </div>
</template>
<style scoped>
.role-voices,
.role-add {
  display: grid;
  gap: 8px;
  min-width: 0;
}
.role-voices dl {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 4px 8px;
  font-size: var(--fs-xs);
  margin: 0;
}
.role-voices dt,
.role-voices small {
  color: var(--text-muted);
}
.role-voices dd {
  margin: 0;
  overflow-wrap: anywhere;
}
.role-voice {
  display: grid;
  grid-template-columns: minmax(40px, 0.8fr) minmax(50px, 1.2fr) auto;
  align-items: center;
  gap: 5px;
}
.role-voice strong {
  overflow-wrap: anywhere;
}
.role-add {
  grid-template-columns: repeat(2, minmax(0, 1fr));
  border-top: 1px solid var(--border-subtle);
  padding-top: 8px;
}
.role-add-field {
  display: grid;
  align-content: start;
  gap: 4px;
  min-width: 0;
  margin: 0;
  color: var(--text-secondary);
  font-size: var(--fs-sm);
}
.role-add-field > input {
  min-width: 0;
  min-height: 36px;
  margin: 0;
  padding: 7px 10px;
  color: var(--text-secondary);
  font: var(--fs-md) / 1.5 var(--font-ui);
}
.role-add > button {
  grid-column: 1/-1;
  justify-self: start;
}
@media (pointer: coarse) {
  .role-add-field > input {
    min-height: 40px;
  }
}
</style>
