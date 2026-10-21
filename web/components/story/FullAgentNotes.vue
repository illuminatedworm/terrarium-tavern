<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { api } from '../../lib/api';
import ContextStrategyControls from './ContextStrategyControls.vue';
import FullAgentPlot from './FullAgentPlot.vue';
import { historyRefLabel } from '../../lib/storyHistory';

const props = defineProps<{ session: any; disabled?: boolean }>();
const emit = defineEmits<{ saved: [session: any] }>();
const records = ref<any>(null);
const loadedRevision = ref<number | null>(null);
const switching = ref(false);
const keepRecent = ref(2);
const loading = ref(false);
const error = ref('');
const showArchived = ref(false);
let readGeneration = 0;
let displayedSession = '';
async function refreshNotes() {
  const sid = props.session.id,
    revision = props.session.revision,
    ticket = ++readGeneration;
  if (displayedSession !== sid) {
    records.value = null;
    loadedRevision.value = null;
    displayedSession = sid;
  }
  loading.value = true;
  error.value = '';
  try {
    const result = await api(`/sessions/${sid}/full-agent`);
    if (props.session.id === sid && props.session.revision === revision && ticket === readGeneration) {
      records.value = result;
      loadedRevision.value = revision;
    }
  } catch (e: any) {
    if (props.session.id === sid && ticket === readGeneration) error.value = e.message;
  } finally {
    if (ticket === readGeneration) loading.value = false;
  }
}
const notes = computed(() =>
  (records.value?.notes || []).filter((n: any) => showArchived.value || n.status === 'active'),
);
watch(() => [props.session.id, props.session.revision], refreshNotes, { immediate: true });
// Keep the previous view while refreshing, but only act on a successfully read revision.
const canSwitch = computed(
  () =>
    !props.disabled &&
    !switching.value &&
    !loading.value &&
    !!records.value &&
    loadedRevision.value === props.session.revision &&
    displayedSession === props.session.id,
);
async function newWindow() {
  if (!canSwitch.value) return;
  const sid = props.session.id;
  switching.value = true;
  error.value = '';
  try {
    const updated = await api(`/sessions/${sid}/full-agent/new-context`, {
      expected_revision: props.session.revision,
      keep_recent: keepRecent.value,
    });
    if (props.session.id === sid) emit('saved', updated);
  } catch (e: any) {
    if (props.session.id === sid) error.value = e.message;
  } finally {
    switching.value = false;
  }
}
const kindLabel: Record<string, string> = { fact: '有据事实', hypothesis: '推测', plan: '计划' };
</script>

<template>
  <section class="full-agent-notes" aria-label="Full Agent 记忆与窗口" :aria-busy="loading">
    <ContextStrategyControls :session="session" :disabled="disabled || switching" />
    <template v-if="session.context_strategy !== 'openai_compact'">
      <p class="inspector-note">
        Full Agent · 工作窗口 {{ session.snapshot.full_agent?.window?.number || 1 }}。 Agent
        自行维护交接笔记、查询历史并换窗。
      </p>
      <section aria-label="交接笔记（handoff）">
        <h3>交接笔记（只读）</h3>
        <p v-if="loading && !records" role="status">正在读取交接笔记…</p>
        <p v-else-if="records?.handoff.text" class="note-content">{{ records.handoff.text }}</p>
        <p v-else-if="records" class="state-empty">尚无交接笔记，Agent 会在需要时整理。</p>
      </section>
      <p class="inspector-note">
        这里显示模型实际使用的交接及来源标注。发现记忆错误时，请在对话中要求 Agent 核对并更新。
      </p>
      <form class="handoff-actions" @submit.prevent="newWindow">
        <label>
          保留最近剧情消息（0–12 条）
          <input
            v-model.number="keepRecent"
            type="number"
            min="0"
            max="12"
            step="1"
            required
            :disabled="disabled || switching"
          />
        </label>
        <button type="submit" :disabled="!canSwitch">
          {{ switching ? '正在换窗…' : '换新窗口' }}
        </button>
      </form>
      <p class="inspector-note">
        手动换窗复用已有交接，默认保留最近 2
        条剧情消息，并扩展到完整交流。退出范围须已有章节覆盖，交接正文和来源引用保持不变。
        长期笔记与完整剧情继续保留。
      </p>
    </template>
    <FullAgentPlot
      :session="session"
      :disabled="disabled || switching"
      :limit="records?.plot_tokens"
      @saved="emit('saved', $event)"
    />
    <p v-if="error" role="alert">{{ error }}</p>
    <div class="notes-heading">
      <h3>长期笔记</h3>
      <button class="quiet" :disabled="loading || switching" @click="refreshNotes">
        {{ loading ? '读取中…' : '刷新' }}
      </button>
    </div>
    <p class="inspector-note">
      {{
        session.channel_origin === 'chatgpt'
          ? '两种上下文方式共用这些笔记，由 Agent 通过工具维护并按需读取。'
          : '这些笔记由 Agent 维护并按需读取，本地换窗不会删除长期笔记。'
      }}
    </p>
    <label><input v-model="showArchived" type="checkbox" />显示已归档笔记</label>
    <details
      v-for="note in notes"
      :key="note.key"
      class="agent-note"
      :class="[`note-${note.kind}`, { archived: note.status === 'archived' }]"
    >
      <summary>
        <span class="note-key">{{ note.key }}</span
        ><span class="note-chip">{{ kindLabel[note.kind] }}</span
        ><span class="note-chip note-status">{{ note.status === 'archived' ? '已归档' : '有效' }}</span
        ><span v-if="note.sources.length" class="note-sources"
          >来源 · {{ historyRefLabel(note.sources) }}</span
        >
        <span class="note-preview">{{ note.content }}</span>
      </summary>
      <p class="note-content">{{ note.content }}</p>
      <small v-if="note.sources.length">原文来源：{{ note.sources.join('、') }}</small>
    </details>
    <p v-if="loading && !records" class="state-empty">正在读取笔记…</p>
    <p v-else-if="records && !notes.length" class="state-empty">
      暂无笔记。需要记住某件事时，可以直接告诉 Agent。
    </p>
  </section>
</template>

<style scoped>
.handoff-actions {
  display: flex;
  align-items: end;
  flex-wrap: wrap;
  gap: 8px;
}
.handoff-actions input {
  width: 5em;
}
.agent-note {
  --note-color: var(--ctx-memory);
  margin-block: 0 10px;
  padding: 0;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: rgb(var(--well-rgb) / 0.35);
  overflow-wrap: anywhere;
  transition: border-color var(--dur);
}
.agent-note:hover,
.agent-note[open] {
  border-color: color-mix(in srgb, var(--note-color) 35%, var(--border-control));
}
.agent-note.note-hypothesis {
  --note-color: var(--ctx-rules);
}
.agent-note.note-plan {
  --note-color: var(--ctx-turn);
}
.agent-note.archived {
  opacity: 0.62;
}
.agent-note summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding: 12px 16px;
  list-style: none;
}
.agent-note summary::-webkit-details-marker {
  display: none;
}
.note-key {
  margin-right: 4px;
  color: var(--note-color);
  font-size: var(--fs-sm);
  font-weight: 500;
}
.note-chip {
  padding: 1px 8px;
  border-radius: var(--radius-pill);
  background: rgb(255 255 255 / 0.05);
  color: var(--text-muted);
  font-size: var(--fs-2xs);
}
.note-status {
  color: var(--success);
}
.archived .note-status {
  color: var(--text-muted);
}
.note-chip.warn {
  color: var(--warning);
}
.note-sources {
  margin-left: auto;
  padding: 1px 10px;
  border: 1px solid var(--border-control);
  border-radius: var(--radius-pill);
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
}
/* Collapsed cards show the first lines; the full text replaces them when open. */
.note-preview {
  display: -webkit-box;
  flex-basis: 100%;
  margin-top: 4px;
  overflow: hidden;
  color: var(--text-primary);
  font-size: var(--fs-md);
  line-height: 1.8;
  white-space: pre-wrap;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  line-clamp: 2;
}
.agent-note[open] .note-preview {
  display: none;
}
.agent-note > p,
.agent-note > small {
  display: block;
  margin: 0;
  padding: 0 16px 12px;
}
.agent-note > small {
  color: var(--text-muted);
}
.notes-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 0;
}
.notes-heading h3 {
  margin: 0;
}
.note-content {
  color: var(--text-primary);
  font-size: var(--fs-md);
  line-height: 1.8;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
