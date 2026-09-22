<script setup lang="ts">
import { watch } from 'vue';
import { notify } from '../../lib/notifications';
const props = defineProps<{ message: string }>();
watch(
  () => props.message,
  (message) => notify(message),
  { immediate: true },
);
</script>
<template></template>
