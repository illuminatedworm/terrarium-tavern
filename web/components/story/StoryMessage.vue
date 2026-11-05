<script setup lang="ts">
import { computed, ref } from 'vue';
import StoryProse from './StoryProse.vue';
import StoryFrame from './StoryFrame.vue';
import ExperimentalRecords from './ExperimentalRecords.vue';
import StorySegmentDivider from './StorySegmentDivider.vue';
import StoryVisuals from './StoryVisuals.vue';
import { messageCharacter, messageContents } from '../../lib/storyPresentation';
import type { StoryContent } from '../../lib/storyPresentation';
import { orderedStoryContents } from '../../lib/visuals';
import { useStoryVisuals } from '../../composables/useStoryVisuals';

const props = defineProps<{
  message: any;
  session: any;
  enabled: boolean;
  hasVisuals?: boolean;
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
const visualAnchor = ref<HTMLElement>();
const showVisuals = computed(
  () =>
    !props.panelOnly &&
    !!props.hasVisuals &&
    !props.session.kemini &&
    props.session.agent_mode === 'full_agent',
);
const { output, error, loading, retry } = useStoryVisuals(
  () => ({ enabled: showVisuals.value, session: props.session.id, message: props.message.id }),
  visualAnchor,
);
const ordered = computed(() => orderedStoryContents(contents.value, showVisuals.value ? output.value : null));
function forwardAction(
  method: string,
  args: any,
  resolve: (value: any) => void,
  reject: (error: any) => void,
  stream: (chunk: string) => void,
) {
  emit('action', method, args, resolve, reject, stream);
}
function composeVisual(text: string) {
  emit(
    'action',
    'visual-input',
    { text },
    () => {},
    () => {},
    () => {},
  );
}
</script>
<template>
  <template v-for="item in ordered.items" :key="item.key">
    <StoryVisuals v-if="item.kind === 'visual'" :spec="item.spec" @compose="composeVisual" />
    <StorySegmentDivider v-else-if="item.content.segmentBreak" />
    <StoryFrame
      v-else-if="item.content.format === 'html'"
      :html="item.content.text"
      :character="character"
      :message="message"
      :session="session"
      :enabled="enabled"
      :expand-first-level="expandFirstLevel"
      :find-in-dialogue="findInDialogue"
      @action="forwardAction"
    />
    <StoryProse
      v-else-if="item.content.text.trim()"
      :text="item.content.text"
      :breaks="message.role === 'user'"
      :session-id="session.id"
      :message-id="message.id"
    />
  </template>
  <ExperimentalRecords v-if="!panelOnly" :parts="message.auxiliary || []" />
  <div v-if="showVisuals" ref="visualAnchor">
    <p v-if="error" class="error" role="alert">
      图形加载失败：{{ error }} <button :disabled="loading" @click="retry">重试</button>
    </p>
    <p v-if="ordered.unaligned" class="hint">
      正文分段记录已失效或缺失，无法还原穿插位置；图形保留在正文后。
    </p>
  </div>
</template>
