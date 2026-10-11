<script setup lang="ts">
import { computed } from 'vue';
import StoryProse from './StoryProse.vue';
import StoryFrame from './StoryFrame.vue';
import { snapshotMacros } from '../../../compat/variable-macros';
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
const character = computed(() => {
  const card =
    typeof props.session.card_data === 'string'
      ? JSON.parse(props.session.card_data)
      : props.session.card_data;
  const document = props.message.snapshot?.content?.document || card?.document || {};
  const data = document.data || document;
  return data.nickname || data.name || '';
});
const contents = computed<StoryContent[]>(() => {
  const parts = (props.panelOnly ? props.message.presentation?.panel : props.message.presentation?.body) || [
    { format: 'markdown', text: props.message.raw || '' },
  ];
  return parts.map((part: StoryContent) => ({
    ...part,
    text: snapshotMacros(part.text, props.message.snapshot?.state, props.message.snapshot?.scopes)
      .replace(/\{\{user\}\}/gi, () => props.session.persona || '用户')
      .replace(/\{\{char\}\}/gi, () => character.value),
  }));
});
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
    <StoryProse v-else-if="content.text.trim()" :text="content.text" :breaks="message.role === 'user'" />
  </template>
</template>
