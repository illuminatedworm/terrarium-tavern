<script setup lang="ts">
import Icon from '../common/Icon.vue';
// The Advisor's icon names map onto the shared icon family.
const names = {
  compass: 'compass',
  close: 'close',
  settings: 'cog',
  send: 'arrow-up',
  chevron: 'chevron-right',
  document: 'file',
  stop: 'stop',
} as const;
defineProps<{ name: keyof typeof names }>();
</script>

<template>
  <Icon :name="names[name]" :size="18" />
</template>
