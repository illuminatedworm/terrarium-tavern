<script setup lang="ts">
import StoryMessage from './StoryMessage.vue';
import StateTree from './StateTree.vue';
import type { Session, StoryMessageRecord } from '../../lib/types';

/** Inspector "MVU 变量" page: the card's status panel plus the raw variable tree. */
defineProps<{
  session: Session;
  /** The latest message carrying a status panel, bound to the current head. */
  panel: StoryMessageRecord | null;
  scriptsEnabled: boolean;
  permissionDisabled: boolean;
}>();
const emit = defineEmits<{
  action: [
    method: string,
    args: any,
    resolve: (value: any) => void,
    reject: (error: any) => void,
    stream: (chunk: string) => void,
  ];
  'enable-scripts': [];
}>();
</script>

<template>
  <section aria-label="MVU 变量">
    <h2>MVU 变量</h2>
    <StoryMessage
      v-if="panel"
      :message="panel"
      panel-only
      expand-first-level
      :session="session"
      :enabled="scriptsEnabled"
      @action="(...args) => emit('action', ...args)"
    />
    <p v-else class="state-empty">此角色卡没有独立状态面板。</p>
    <button v-if="panel && !scriptsEnabled" :disabled="permissionDisabled" @click="emit('enable-scripts')">
      启用此对话的脚本
    </button>
    <details :key="session.id" open>
      <summary>原始 MVU 变量</summary>
      <StateTree :value="session.snapshot.state.stat_data || session.snapshot.state" />
    </details>
  </section>
</template>
