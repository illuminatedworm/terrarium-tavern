<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { api } from '../../lib/api';
import { renderStoryText } from '../../lib/storyText';
const props = defineProps<{ text: string; breaks?: boolean; sessionId?: string; messageId?: string }>();
const rendered = computed(() => renderStoryText(props.text, props.breaks));
const prepared = ref('');
const imageError = ref('');
const loadingImages = ref(false);
let request = new AbortController();
async function prepare() {
  request.abort();
  const controller = (request = new AbortController());
  imageError.value = '';
  loadingImages.value = false;
  const html = rendered.value;
  if (!props.sessionId || !props.messageId) {
    prepared.value = html;
    return;
  }
  const template = document.createElement('template');
  template.innerHTML = html;
  let remote = false;
  template.content.querySelectorAll('img, source, image, video, [style]').forEach((el) => {
    const attributes = ['style'];
    if (el.tagName.toLowerCase() === 'img') attributes.push('src', 'srcset');
    if (el.tagName.toLowerCase() === 'source') attributes.push('srcset');
    if (el.tagName.toLowerCase() === 'image') attributes.push('href', 'xlink:href');
    if (el.tagName.toLowerCase() === 'video') attributes.push('poster');
    for (const attr of attributes) {
      if (/https?:\/\//i.test(el.getAttribute(attr) || '')) {
        el.removeAttribute(attr);
        remote = true;
      }
    }
  });
  prepared.value = remote ? template.innerHTML : html;
  if (!remote) return;
  loadingImages.value = true;
  try {
    const result = await api(
      `/sessions/${props.sessionId}/images/markup`,
      {
        html,
        message_id: props.messageId,
      },
      'POST',
      controller.signal,
    );
    if (!controller.signal.aborted) prepared.value = result.html;
  } catch (error) {
    if (!controller.signal.aborted) imageError.value = String(error);
  } finally {
    if (!controller.signal.aborted) loadingImages.value = false;
  }
}
watch(
  () => [rendered.value, props.sessionId, props.messageId],
  () => void prepare(),
  { immediate: true },
);
onBeforeUnmount(() => request.abort());
</script>
<template>
  <div class="prose" v-html="prepared" />
  <p v-if="loadingImages" class="muted" role="status">正在加载图片…</p>
  <div v-if="imageError" class="error" role="alert">
    图片加载失败：{{ imageError }} <button @click="prepare">重试图片</button>
  </div>
</template>
