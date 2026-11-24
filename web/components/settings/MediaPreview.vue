<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue';
import { channelLabels, type Channel } from '../../lib/channels';
import type { MediaKind, MediaProtocols, Profile } from '../../lib/media';
import { mediaPreview } from '../../lib/api';
import MediaVoiceSelect from './MediaVoiceSelect.vue';
const props = defineProps<{
  kind: MediaKind;
  channel: Channel;
  profile: Profile;
  catalog: MediaProtocols;
  providerName?: string;
  missingKey: boolean;
  flush?: () => Promise<unknown>;
}>();
const sample = ref(
  props.kind === 'speech'
    ? '夜色渐深，窗边还亮着一盏灯。'
    : '黄昏，一盏暖灯照亮木屋的窗台，窗外是安静的森林。',
);
const voice = ref('');
const url = ref('');
const busy = ref(false);
const error = ref('');
let pending: AbortController | undefined;
function reset() {
  pending?.abort();
  pending = undefined;
  busy.value = false;
  error.value = '';
  if (url.value) URL.revokeObjectURL(url.value);
  url.value = '';
}
watch(
  [
    () => ('default_voice' in props.profile ? props.profile.default_voice : ''),
    () => props.profile.provider_id,
    () => props.profile.model,
  ],
  () => {
    voice.value = 'default_voice' in props.profile ? props.profile.default_voice : '';
  },
  { immediate: true },
);
watch([() => JSON.stringify(props.profile), () => props.channel, sample, voice], reset, { flush: 'sync' });
onBeforeUnmount(reset);
async function generate() {
  if (busy.value) return;
  reset();
  const controller = new AbortController();
  pending = controller;
  busy.value = true;
  const data = {
    kind: props.kind,
    channel: props.channel,
    profile: props.profile,
    ...(props.kind === 'speech' ? { text: sample.value, voice: voice.value } : { prompt: sample.value }),
  };
  try {
    await props.flush?.();
    if (controller.signal.aborted) return;
    const result = await mediaPreview(data, controller.signal);
    if (!controller.signal.aborted) url.value = URL.createObjectURL(result);
  } catch (e) {
    if (!controller.signal.aborted) error.value = String(e);
  } finally {
    if (pending === controller) {
      pending = undefined;
      busy.value = false;
    }
  }
}
</script>
<template>
  <div class="media-sample">
    <label>{{ kind === 'speech' ? '试听台词' : '试图提示词' }}<textarea v-model="sample" rows="2" /></label>
    <div v-if="'default_voice' in profile" class="media-choice">
      <span>试听音色</span>
      <MediaVoiceSelect v-model="voice" :profile="profile" :catalog="catalog" label="试听音色" />
    </div>
    <small
      >{{ channelLabels[channel] }} · {{ providerName || '未选择提供商' }} ·
      {{ profile.model || '未选择模型' }}。生成将使用此渠道额度。</small
    >
    <button
      type="button"
      :disabled="
        busy ||
        !providerName ||
        missingKey ||
        !profile.model.trim() ||
        !sample.trim() ||
        (kind === 'speech' && !voice.trim())
      "
      @click="generate"
    >
      {{ busy ? '正在生成…' : kind === 'speech' ? '生成试听' : '生成测试图' }}
    </button>
    <span v-if="busy" role="status">等待服务返回…</span>
    <p v-if="error" role="alert">{{ error }}</p>
    <audio v-if="url && kind === 'speech'" :src="url" controls aria-label="试听结果" />
    <img v-if="url && kind === 'image'" :src="url" alt="本次测试生成的插图" />
  </div>
</template>
<style scoped>
.media-sample,
.media-choice {
  display: grid;
  gap: 8px;
  min-width: 0;
}
.media-sample {
  padding-top: 10px;
}
.media-sample small,
.media-sample span {
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
.media-sample button {
  justify-self: start;
}
.media-sample img,
.media-sample audio {
  width: 100%;
  max-height: 240px;
  object-fit: contain;
}
.media-sample [role='alert'] {
  color: var(--danger);
  overflow-wrap: anywhere;
}
</style>
