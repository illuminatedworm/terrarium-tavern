<script setup lang="ts">
import { computed } from 'vue';
import StoryProse from './StoryProse.vue';
import StoryFrame from './StoryFrame.vue';
import ExperimentalRecords from './ExperimentalRecords.vue';
import { messageCharacter, messageContents } from '../../lib/storyPresentation';
import type { StoryContent } from '../../lib/storyPresentation';

const props = defineProps<{
  message: any;
  session: any;
  enabled: boolean;
  panelOnly?: boolean;
  expandFirstLevel?: boolean;
  findInDialogue?: boolean;
}>();
const emit = defineEmits<{
  action: [
    method: string,
    args: any,
    resolve: (value: any) => void,
    reject: (error: any) => void,
    stream: (chunk: string) => void,
  ];
}>();
const character = computed(() => messageCharacter(props.message, props.session));
const contents = computed<StoryContent[]>(() =>
  messageContents(props.message, props.session, { panel: props.panelOnly }),
);
function forwardAction(
  method: string,
  args: any,
  resolve: (value: any) => void,
  reject: (error: any) => void,
  stream: (chunk: string) => void,
) {
  emit('action', method, args, resolve, reject, stream);
}
</script>
<template>
  <template v-for="(content, index) in contents" :key="index">
    <StoryFrame
      v-if="content.format === 'html'"
      :html="content.text"
      :character="character"
      :message="message"
      :session="session"
      :enabled="enabled"
      :expand-first-level="expandFirstLevel"
      :find-in-dialogue="findInDialogue"
      @action="forwardAction"
    />
    <StoryProse
      v-else-if="content.text.trim()"
      :text="content.text"
      :breaks="message.role === 'user'"
      :session-id="session.id"
      :message-id="message.id"
    />
  </template>
  <ExperimentalRecords v-if="!panelOnly" :parts="message.auxiliary || []" />
</template>
