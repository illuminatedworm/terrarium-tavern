<script setup lang="ts">
import { computed } from 'vue';
import type { AgentActivity } from '../../lib/agentActivity';
import { draftStoryContents } from '../../lib/visuals';
import StoryProse from './StoryProse.vue';
import StorySegmentDivider from './StorySegmentDivider.vue';
import StoryVisuals from './StoryVisuals.vue';

const props = defineProps<{ segments: string[]; activities: AgentActivity[] }>();
const emit = defineEmits<{ compose: [text: string] }>();
const items = computed(() => draftStoryContents(props.segments, props.activities));
</script>

<template>
  <template v-for="item in items" :key="item.key">
    <StoryVisuals
      v-if="item.kind === 'visual'"
      :spec="item.spec"
      preview
      @compose="emit('compose', $event)"
    />
    <template v-else>
      <StorySegmentDivider v-if="item.divider" />
      <StoryProse :text="item.text" />
    </template>
  </template>
</template>
