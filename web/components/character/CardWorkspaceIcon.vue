<script setup lang="ts">
import Icon from '../common/Icon.vue';
// The Card Editor's historical icon names map onto the shared icon family.
const names = {
  pen: 'pencil',
  plus: 'plus',
  up: 'arrow-up',
  down: 'arrow-down',
  message: 'chat',
  sliders: 'sliders',
  file: 'file',
  chevron: 'chevron-right',
  book: 'book',
  trash: 'trash',
  person: 'user',
  close: 'close',
} as const;
defineProps<{ name: keyof typeof names }>();
</script>

<template>
  <Icon :name="names[name]" :size="18" :stroke="1.7" />
</template>
