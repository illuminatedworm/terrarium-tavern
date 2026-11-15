<script setup lang="ts">
import { computed } from 'vue';
import Icon from '../common/Icon.vue';
import StoryNotes from './StoryNotes.vue';
import type { Session } from '../../lib/types';

/** Inspector "场景" page: notes for the open branch. */
const props = defineProps<{
  session: Session;
  /** `storyLocked` of the open story. */
  locked: boolean;
  loreDisabled: boolean;
}>();
const emit = defineEmits<{
  'edit-lore': [];
  saved: [session: Session];
}>();
const summary = computed(() => {
  const compact = props.session.context_strategy === 'openai_compact';
  return compact ? '云端 compact' : '工作窗口 ' + (props.session.snapshot.story?.window?.number || 1);
});
</script>

<template>
  <section class="scene-memory" aria-label="场景">
    <header class="scene-heading">
      <h2><Icon name="map" :size="20" />场景</h2>
      <button :disabled="loreDisabled" @click="emit('edit-lore')">编辑对话世界书</button>
      <p class="scene-summary">{{ summary }}</p>
    </header>
    <StoryNotes :key="session.id" :session="session" :disabled="locked" @saved="emit('saved', $event)" />
  </section>
</template>
