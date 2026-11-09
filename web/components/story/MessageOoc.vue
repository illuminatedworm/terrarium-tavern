<script setup lang="ts">
/** The user's out-of-character text inside their message bubble, apart from the story. */
defineProps<{ text: string }>();
</script>

<template>
  <section class="message-ooc" aria-label="场外发言">
    <small>场外</small>
    <p>{{ text }}</p>
  </section>
</template>
