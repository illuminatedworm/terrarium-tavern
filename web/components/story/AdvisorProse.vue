<script setup lang="ts">
import { computed } from 'vue';
import { renderStoryText } from '../../lib/storyText';
const props = defineProps<{ text: string }>();
const html = computed(() => {
  const root = document.createElement('div');
  root.innerHTML = renderStoryText(props.text);
  root.querySelectorAll('a[href]').forEach((link) => {
    link.setAttribute('target', '_blank');
    link.setAttribute('rel', 'noopener noreferrer');
  });
  return root.innerHTML;
});
</script>
<template><div class="prose" v-html="html" /></template>
