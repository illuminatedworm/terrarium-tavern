<script setup lang="ts">
import Icon from '../common/Icon.vue';
import StoryNotes from './StoryNotes.vue';
import StateTree from './StateTree.vue';
import type { Session } from '../../lib/types';

/** "变量与记忆" dialog for the current branch. */
defineProps<{
  session: Session;
  disabled: boolean;
}>();
const emit = defineEmits<{
  saved: [session: Session];
}>();
export type StateTab = 'variables' | 'memory' | 'content';
const tabs: { id: StateTab; label: string }[] = [
  { id: 'variables', label: '变量' },
  { id: 'memory', label: '记忆' },
  { id: 'content', label: '设定演化' },
];
/** Owned by the app so reopening the dialog returns to the last page. */
const tab = defineModel<StateTab>('tab', { required: true });
</script>

<template>
  <div class="state-panel-head">
    <h2 class="panel-title"><Icon name="braces" :size="19" />变量与记忆</h2>
    <nav class="panel-tabs" aria-label="变量与记忆分页">
      <button
        v-for="item in tabs"
        :key="item.id"
        :class="{ active: tab === item.id }"
        :aria-pressed="tab === item.id"
        @click="tab = item.id"
      >
        {{ item.label }}
      </button>
    </nav>
  </div>
  <p class="muted state-panel-note">仅显示当前剧情分支。切换分支会一同恢复变量与记忆。</p>
  <div v-if="tab === 'variables'" class="state-cards">
    <StateTree :value="session.snapshot.state.stat_data || session.snapshot.state" />
  </div>
  <StoryNotes
    v-else-if="tab === 'memory'"
    :session="session"
    :disabled="disabled"
    @saved="emit('saved', $event)"
  />
  <div v-else class="state-cards"><StateTree :value="session.snapshot.content" /></div>
</template>
