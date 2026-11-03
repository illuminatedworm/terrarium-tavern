<script setup lang="ts">
import { computed } from 'vue';
import Icon from '../common/Icon.vue';
import StoryMessage from './StoryMessage.vue';
import StateTree from './StateTree.vue';
import type { Session, StoryMessageRecord } from '../../lib/types';

/** Inspector "MVU 变量" page: the card's status panel plus the raw variable tree. */
const props = defineProps<{
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
const variables = computed(() => props.session.snapshot.state.stat_data || props.session.snapshot.state);
const count = computed(() =>
  variables.value && typeof variables.value === 'object' ? Object.keys(variables.value).length : 0,
);
const summary = computed(() =>
  [
    props.panel ? (props.scriptsEnabled ? '卡片状态面板' : '卡片状态面板 · 脚本未启用') : '无独立状态面板',
    `${count.value} 个顶层变量`,
  ].join(' · '),
);
</script>

<template>
  <section class="mvu-inspector" aria-label="MVU 变量">
    <header class="mvu-head">
      <h2><Icon name="braces" :size="20" />MVU 变量</h2>
      <p>{{ summary }}</p>
    </header>
    <StoryMessage
      v-if="panel"
      :message="panel"
      panel-only
      expand-first-level
      :session="session"
      :enabled="scriptsEnabled"
      @action="(...args) => emit('action', ...args)"
    />
    <button v-if="panel && !scriptsEnabled" :disabled="permissionDisabled" @click="emit('enable-scripts')">
      启用此对话的脚本
    </button>
    <details :key="session.id" class="mvu-raw" open>
      <summary>
        <Icon name="chevron-right" :size="14" class="mvu-raw-chevron" />原始变量<span>{{ count }}</span>
      </summary>
      <div class="mvu-raw-body">
        <StateTree :value="variables" />
      </div>
    </details>
  </section>
</template>
