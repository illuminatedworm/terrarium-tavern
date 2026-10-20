<script setup lang="ts">
import { computed } from 'vue';
import FullAgentNotes from './FullAgentNotes.vue';
import ContextStrategyControls from './ContextStrategyControls.vue';
import SceneNotes from './SceneNotes.vue';
import type { Session } from '../../lib/types';

/** Inspector "场景" page: Full Agent notes, or the Tracker-maintained memory. */
const props = defineProps<{
  session: Session;
  fullAgent: boolean;
  /** `storyLocked` of the open story. */
  locked: boolean;
  loreDisabled: boolean;
  contextBusy: boolean;
  /** A memory rebuild is being estimated or submitted. */
  submitting: boolean;
  updating?: string | null;
  progress: string;
  character: string;
  saving: boolean;
}>();
const emit = defineEmits<{
  'edit-lore': [];
  saved: [session: Session];
  refresh: [];
  change: [value: any];
  cancel: [];
  locate: [source: string, excerpt: string];
}>();
const memoryDisabled = computed(() => props.locked || props.contextBusy || props.submitting);
</script>

<template>
  <section class="scene-memory" aria-label="场景">
    <div class="scene-heading">
      <h2>场景</h2>
      <button :disabled="loreDisabled" @click="emit('edit-lore')">编辑对话世界书</button>
    </div>
    <FullAgentNotes
      v-if="fullAgent"
      :key="session.id"
      :session="session"
      :disabled="locked"
      @saved="emit('saved', $event)"
    />
    <template v-else>
      <ContextStrategyControls :session="session" :disabled="locked || submitting" />
      <p class="inspector-note">
        这是当前对话的记忆上下文，由后台 Tracker 整理、Roleplay Runtime
        保存。记忆随剧情进度保存，继承进度时会带入新对话。
      </p>
      <p v-if="updating" class="scene-update-status" role="status">
        {{ '正在根据当前对话重新生成记忆…' }}
      </p>
      <div class="scene-section-heading">
        <h3>记忆</h3>
        <button
          :disabled="memoryDisabled"
          :aria-busy="updating === 'memory'"
          @click="emit('refresh')"
          aria-label="重新生成记忆"
        >
          <span v-if="updating === 'memory'" class="scene-update-spinner" aria-hidden="true"></span
          >{{ updating === 'memory' ? '更新中…' : '↻ 重新生成' }}
        </button>
      </div>
      <SceneNotes
        :key="`${session.id}:memory`"
        kind="memory"
        :snapshot="session.snapshot"
        :memory-view="session.memory_view"
        :archived="session.archived_memory"
        :progress="progress"
        @change="emit('change', $event)"
        @cancel="emit('cancel')"
        :messages="session.messages"
        :persona="session.persona"
        :character="character"
        :disabled="memoryDisabled"
        :saving="saving"
        @locate="(source, excerpt) => emit('locate', source, excerpt)"
      />
    </template>
  </section>
</template>
