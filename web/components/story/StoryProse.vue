<script setup lang="ts">
import { computed } from 'vue';
import { renderStoryText } from '../../lib/storyText';
const props = defineProps<{ text: string; breaks?: boolean }>();
const rendered = computed(() => renderStoryText(props.text, props.breaks));
</script>
<template><div class="prose" v-html="rendered" /></template>
