<script setup lang="ts">
import StoryNotes from './StoryNotes.vue';
import type { Session } from '../../lib/types';

/** "窗口与笔记" dialog of a story. */
defineProps<{ session: Session; channel: string; model: string; disabled: boolean }>();
const emit = defineEmits<{ saved: [session: Session] }>();
</script>

<template>
  <h2>窗口与笔记</h2>
  <p class="muted">{{ channel }} · {{ model }}</p>
  <StoryNotes :session="session" :disabled="disabled" @saved="emit('saved', $event)" />
</template>
