<script setup lang="ts">
import { ref } from 'vue';
import StoryHistory from './StoryHistory.vue';
defineProps<{
  messages: any[];
  scroller?: HTMLElement;
  editingId?: string;
  following?: boolean;
  retainInteractive?: boolean;
}>();
const emit = defineEmits<{ reading: [id: string] }>();
const history = ref<InstanceType<typeof StoryHistory>>();
defineExpose({
  jumpTo: (id: string) => history.value?.jumpTo(id),
  jumpBoundary: (edge: 'start' | 'end') => history.value?.jumpBoundary(edge),
  releaseAnchor: () => history.value?.releaseAnchor(),
});
</script>

<template>
  <div class="half-agent-dialogue">
    <StoryHistory
      ref="history"
      :messages="messages"
      :scroller="scroller"
      :editing-id="editingId"
      :following="following"
      :retain-interactive="retainInteractive"
      @reading="emit('reading', $event)"
    >
      <template #default="{ message }">
        <slot name="before-message" :message="message" />
        <slot name="message" :message="message" />
      </template>
    </StoryHistory>
    <slot name="pending" />
    <slot name="draft" />
    <slot name="waiting" />
  </div>
</template>
