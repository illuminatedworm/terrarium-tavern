<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { api } from '../../lib/api';
import type { MediaDefaults, MediaProtocols } from '../../lib/media';
import type { Provider } from '../../lib/settingsDocument';
import MediaGenerationFields from './MediaGenerationFields.vue';
import Icon from '../common/Icon.vue';

defineProps<{ value: MediaDefaults; providers: Provider[]; flush: () => Promise<unknown> }>();
defineEmits<{ providers: [] }>();
const protocols = ref<MediaProtocols>();
const error = ref('');
async function load() {
  error.value = '';
  try {
    protocols.value = await api('/media/protocols');
  } catch (failure) {
    error.value = String(failure);
  }
}
onMounted(load);
</script>

<template>
  <div class="media-page-tools">
    <p class="muted">故事可分别沿用这里的配音和插图设置。生成权限在故事侧栏开启。</p>
    <button type="button" class="quiet" @click="$emit('providers')">
      <Icon name="cog" :size="14" />管理提供商
    </button>
  </div>
  <div v-if="error" role="alert">{{ error }} <button type="button" @click="load">重新加载</button></div>
  <p v-else-if="!protocols" role="status">正在加载媒体协议…</p>
  <div v-else class="local-model-grid">
    <MediaGenerationFields
      v-for="kind in ['speech', 'image'] as const"
      :key="kind"
      :data-setting-path="'media.' + kind"
      :kind="kind"
      :route="value[kind]"
      :catalog="protocols"
      :providers="providers"
      :flush="flush"
    />
  </div>
</template>
<style scoped>
.media-page-tools {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px 16px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.media-page-tools .muted {
  margin: 0;
}
.media-page-tools button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}
.local-model-grid {
  align-items: start;
}
</style>
