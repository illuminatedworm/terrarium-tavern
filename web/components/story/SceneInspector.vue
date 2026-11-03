<script setup lang="ts">
import { computed } from 'vue';
import FieldHelp from '../common/FieldHelp.vue';
import Icon from '../common/Icon.vue';
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
const summary = computed(() => {
  const compact = props.session.context_strategy === 'openai_compact';
  if (props.fullAgent)
    return `Full Agent · ${compact ? '云端 compact' : `工作窗口 ${(props.session.snapshot as any).full_agent?.window?.number || 1}`}`;
  return `${compact ? '云端 compact' : '本地滚动窗口'} · 记忆由 Tracker 整理`;
});
</script>

<template>
  <section class="scene-memory" aria-label="场景">
    <header class="scene-heading">
      <h2><Icon name="map" :size="20" />场景</h2>
      <button :disabled="loreDisabled" @click="emit('edit-lore')">编辑对话世界书</button>
      <p class="scene-summary">{{ summary }}</p>
    </header>
    <FullAgentNotes
      v-if="fullAgent"
      :key="session.id"
      :session="session"
      :disabled="locked"
      @saved="emit('saved', $event)"
    />
    <template v-else>
      <ContextStrategyControls :session="session" :disabled="locked || submitting" />
      <p v-if="updating" class="scene-update-status" role="status">正在根据当前对话重新生成记忆…</p>
      <section class="scene-block scene-memory-block" aria-label="记忆">
        <div class="scene-block-head">
          <h3>
            记忆<FieldHelp
              tooltip="这是当前对话的记忆上下文，由后台 Tracker 整理、Roleplay Runtime 保存。记忆随剧情进度保存，继承进度时会带入新对话。"
            />
          </h3>
          <button
            :disabled="memoryDisabled"
            :aria-busy="updating === 'memory'"
            aria-label="重新生成记忆"
            @click="emit('refresh')"
          >
            <span v-if="updating === 'memory'" class="scene-update-spinner" aria-hidden="true"></span
            ><Icon v-else name="refresh" :size="14" />{{ updating === 'memory' ? '更新中…' : '重新生成' }}
          </button>
        </div>
        <div class="scene-block-body">
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
        </div>
      </section>
    </template>
  </section>
</template>
