<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue';
import { streamApi } from '../../lib/api';
import { renderStoryText } from '../../lib/storyText';
const props = defineProps<{ text: string; breaks?: boolean; sessionId?: string; messageId?: string }>();
const rendered = computed(() => renderStoryText(props.text, props.breaks));
const prepared = ref('');
const root = ref<HTMLElement>();
const total = ref(0),
  loaded = ref(0),
  failures = ref<string[]>([]);
let request = new AbortController();

// Remote images are never fetched by the reader itself. Their attributes wait in
// data-tavern-* until the host streams each image back, in reading order.
function imageAttributes(el: Element) {
  const tag = el.tagName.toLowerCase();
  return [
    'style',
    ...(tag === 'img' ? ['src', 'srcset'] : []),
    ...(tag === 'source' ? ['srcset'] : []),
    ...(tag === 'image' ? ['href', 'xlink:href'] : []),
    ...(tag === 'video' ? ['poster'] : []),
  ];
}
const pendingName = (attr: string) => 'data-tavern-' + attr.replace(':', '-');
function holdRemote(html: string, matches: (value: string) => boolean) {
  const template = document.createElement('template');
  let held = false;
  template.innerHTML = html;
  template.content.querySelectorAll('img, source, image, video, [style]').forEach((el) => {
    for (const attr of imageAttributes(el)) {
      const value = el.getAttribute(attr);
      if (value === null || !matches(value)) continue;
      el.setAttribute(pendingName(attr), value);
      el.removeAttribute(attr);
      held = true;
    }
  });
  return held ? template.innerHTML : html;
}
function reveal(urls: string[], resolved: Map<string, string>) {
  root.value?.querySelectorAll('img, source, image, video, [style]').forEach((el) => {
    for (const attr of imageAttributes(el)) {
      const original = el.getAttribute(pendingName(attr));
      if (original === null) continue;
      const waiting = urls.filter((url) => original.includes(url));
      if (waiting.some((url) => !resolved.has(url))) continue;
      el.setAttribute(
        attr,
        waiting.reduce((value, url) => value.split(url).join(resolved.get(url)!), original),
      );
      el.removeAttribute(pendingName(attr));
    }
  });
}
async function prepare() {
  request.abort();
  const controller = (request = new AbortController());
  total.value = loaded.value = 0;
  failures.value = [];
  const html = rendered.value;
  const remote = (value: string) => /https?:\/\//i.test(value);
  prepared.value = props.sessionId && props.messageId ? holdRemote(html, remote) : html;
  if (prepared.value === html) return;
  let urls: string[] = [];
  const resolved = new Map<string, string>();
  try {
    await streamApi(
      `/sessions/${props.sessionId}/images/stream`,
      { html, message_id: props.messageId },
      controller.signal,
      async (line) => {
        if (controller.signal.aborted) return;
        if (line.html !== undefined) {
          urls = line.urls;
          total.value = urls.length;
          prepared.value = holdRemote(line.html, (value) => urls.some((url) => value.includes(url)));
          await nextTick();
        } else if (line.error !== undefined) {
          failures.value = [...failures.value, line.error];
        } else {
          resolved.set(line.url, line.data);
          loaded.value = resolved.size;
          reveal(urls, resolved);
        }
      },
    );
  } catch (error) {
    if (!controller.signal.aborted) failures.value = [...failures.value, String(error)];
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
  <div ref="root" class="prose" v-html="prepared" />
  <p v-if="loaded + failures.length < total" class="muted" role="status">
    正在加载图片 {{ loaded }}/{{ total }}
  </p>
  <div v-if="failures.length" class="error" role="alert">
    {{ failures.length }} 处图片加载失败：{{ failures[0] }} <button @click="prepare">重试图片</button>
  </div>
</template>
