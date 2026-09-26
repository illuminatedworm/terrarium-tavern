<script setup lang="ts">
defineProps<{ messages: any[] }>();
</script>

<template>
  <div class="half-agent-dialogue">
    <template v-for="message in messages" :key="message.id">
      <slot name="before-message" :message="message" />
      <slot name="message" :message="message" />
    </template>
    <slot name="pending" />
    <slot name="draft" />
    <slot name="waiting" />
  </div>
</template>
