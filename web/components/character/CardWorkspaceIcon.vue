<script setup lang="ts">
defineProps<{
  name: 'pen' | 'plus' | 'up' | 'down' | 'message' | 'sliders' | 'file' | 'chevron' | 'book' | 'trash';
}>();
</script>

<template>
  <svg
    width="18"
    height="18"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.7"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <template v-if="name === 'pen'">
      <path d="m15 4 5 5M4 20l5-1L20 8a2.1 2.1 0 0 0-5-5L4 14Z" />
      <path d="m4 14 5 5" />
    </template>
    <path v-else-if="name === 'plus'" d="M12 5v14M5 12h14" />
    <path v-else-if="name === 'up'" d="M12 19V5m-6 6 6-6 6 6" />
    <path v-else-if="name === 'down'" d="M12 5v14m-6-6 6 6 6-6" />
    <path v-else-if="name === 'message'" d="M20 11a8 8 0 0 1-8 8H5l-3 3V11a9 9 0 0 1 18 0Z" />
    <template v-else-if="name === 'sliders'">
      <path d="M3 6h4m4 0h10M3 18h10m4 0h4" />
      <circle cx="9" cy="6" r="2" />
      <circle cx="15" cy="18" r="2" />
    </template>
    <template v-else-if="name === 'file'">
      <path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8Z" />
      <path d="M14 3v5h5M9 13h6M9 17h6" />
    </template>
    <template v-else-if="name === 'book'">
      <path d="M12 5c-3-2-6-2-9-1v15c3-1 6-1 9 1 3-2 6-2 9-1V4c-3-1-6-1-9 1Zm0 0v15" />
    </template>
    <template v-else-if="name === 'trash'">
      <path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7" />
    </template>
    <path v-else d="m9 5 7 7-7 7" />
  </svg>
</template>

<style scoped>
svg {
  flex: 0 0 auto;
  vertical-align: middle;
}
</style>
