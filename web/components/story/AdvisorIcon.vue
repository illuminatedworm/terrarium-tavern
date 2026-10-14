<script setup lang="ts">
defineProps<{ name: 'compass' | 'close' | 'settings' | 'send' | 'chevron' | 'document' | 'stop' }>();
</script>
<template>
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.7"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <template v-if="name === 'compass'">
      <circle cx="12" cy="12" r="9.5" />
      <path d="m16.5 7.5-2.8 6.2-6.2 2.8 2.8-6.2Z" />
      <path d="m10.3 10.3 3.4 3.4" />
    </template>
    <path v-else-if="name === 'close'" d="m6 6 12 12M18 6 6 18" />
    <template v-else-if="name === 'settings'">
      <path
        d="m9 3-.7 2.3-2 .9-2.2-.5L2.7 8.2l1.5 1.9v2.3l-1.5 1.9 1.4 2.5 2.2-.5 2 .9L9 20h3l.7-2.8 2-.9 2.2.5 1.4-2.5-1.5-1.9v-2.3l1.5-1.9-1.4-2.5-2.2.5-2-.9L12 3Z"
        transform="translate(1.5 .5)"
      />
      <circle cx="12" cy="12" r="3" />
    </template>
    <path v-else-if="name === 'send'" d="m4 3 17 9-17 9 3-9-3-9Zm3 9h14" />
    <path v-else-if="name === 'chevron'" d="m9 5 7 7-7 7" />
    <template v-else-if="name === 'document'">
      <path d="M14 3H5v18h14V8Z" />
      <path d="M14 3v5h5M8 12h8M8 16h5" />
    </template>
    <rect v-else x="6" y="6" width="12" height="12" rx="2" />
  </svg>
</template>
