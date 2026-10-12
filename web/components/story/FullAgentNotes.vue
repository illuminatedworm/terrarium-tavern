<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { api } from '../../lib/api';
import ContextStrategyControls from './ContextStrategyControls.vue';
import FullAgentPlot from './FullAgentPlot.vue';

const props = defineProps<{ session: any; disabled?: boolean }>();
const emit = defineEmits<{ saved: [session: any] }>();
const handoff = ref('');
const records = ref<any>(null);
const saving = ref(false);
const error = ref('');
const showArchived = ref(false);
let readGeneration = 0;
let displayedSession = '';
async function refreshNotes() {
  const sid = props.session.id,
    ticket = ++readGeneration;
  try {
    const result = await api(`/sessions/${sid}/full-agent`);
    if (props.session.id === sid && ticket === readGeneration) records.value = result;
  } catch (e: any) {
    if (props.session.id === sid && ticket === readGeneration) error.value = e.message;
  }
}
const notes = computed(() =>
  (records.value?.notes || []).filter((n: any) => showArchived.value || n.status === 'active'),
);
watch(
  () => [props.session.id, props.session.revision],
  async () => {
    handoff.value = props.session.snapshot.full_agent?.handoff?.text || '';
    if (displayedSession !== props.session.id) records.value = null;
    displayedSession = props.session.id;
    error.value = '';
    await refreshNotes();
  },
  { immediate: true },
);
async function save(newWindow: boolean) {
  const sid = props.session.id;
  saving.value = true;
  error.value = '';
  try {
    const updated = await api(
      `/sessions/${sid}/full-agent/context`,
      {
        expected_revision: props.session.revision,
        content: handoff.value,
        new_window: newWindow,
        keep_recent: 0,
      },
      'PUT',
    );
    if (props.session.id === sid) emit('saved', updated);
  } catch (e: any) {
    if (props.session.id === sid) error.value = e.message;
  } finally {
    saving.value = false;
  }
}
const kindLabel: Record<string, string> = { fact: '有据事实', hypothesis: '推测', plan: '计划' };
</script>

<template>
  <section class="full-agent-notes" aria-label="Full Agent 记忆与窗口">
    <ContextStrategyControls :session="session" :disabled="disabled || saving" />
    <template v-if="session.context_strategy !== 'openai_compact'">
      <p class="inspector-note">
        Full Agent · 工作窗口 {{ session.snapshot.full_agent?.window?.number || 1 }}。 Agent
        自行维护交接笔记、查询历史并换窗。
      </p>
      <label
        >交接笔记（handoff）
        <textarea
          v-model="handoff"
          rows="7"
          :disabled="disabled || saving"
          placeholder="当前场景、重要经历、用户约束与未决线索…"
        />
      </label>
      <p v-if="records?.handoff?.stale" role="status">原始剧情已编辑，这份交接笔记需要核对。</p>
      <div class="handoff-actions">
        <button :disabled="disabled || saving || !handoff.trim()" @click="save(false)">保存交接笔记</button>
        <button :disabled="disabled || saving || !handoff.trim()" @click="save(true)">保存并换新窗口</button>
      </div>
      <p class="inspector-note">
        手动换窗后携带交接笔记、基础设定和已开启的剧情规划。长期笔记独立保留；剧情原文可搜索，工具过程可在记录中查看。
      </p>
    </template>
    <FullAgentPlot
      :session="session"
      :disabled="disabled || saving"
      :limit="records?.plot_tokens"
      @saved="emit('saved', $event)"
    />
    <p v-if="error" role="alert">{{ error }}</p>
    <div class="notes-heading">
      <h3>长期笔记</h3>
      <button class="quiet" @click="refreshNotes">刷新</button>
    </div>
    <p class="inspector-note">
      {{
        session.channel_origin === 'chatgpt'
          ? '两种上下文方式共用这些笔记，由 Agent 通过工具维护并按需读取。'
          : '这些笔记由 Agent 维护并按需读取，本地换窗不会删除长期笔记。'
      }}
    </p>
    <label><input v-model="showArchived" type="checkbox" />显示已归档笔记</label>
    <details v-for="note in notes" :key="note.key" class="agent-note">
      <summary>
        <span>{{ note.key }}</span
        ><span class="note-kind">{{ kindLabel[note.kind] }}</span
        ><span class="note-status">{{ note.status === 'archived' ? '已归档' : '有效' }}</span>
      </summary>
      <p v-if="note.stale" role="status">来源已编辑或不在当前分支，请核对。</p>
      <p class="note-content">{{ note.content }}</p>
      <small v-if="note.sources.length">原文来源：{{ note.sources.join('、') }}</small>
    </details>
    <p v-if="!records" class="state-empty">正在读取笔记…</p>
    <p v-else-if="!notes.length" class="state-empty">暂无笔记。需要记住某件事时，可以直接告诉 Agent。</p>
  </section>
</template>

<style scoped>
.handoff-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
textarea {
  width: 100%;
  resize: vertical;
}
.agent-note {
  margin-block: 0;
  padding: 14px 12px;
  border-bottom: 1px solid var(--border-subtle);
  overflow-wrap: anywhere;
}
.agent-note summary {
  cursor: pointer;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  gap: 16px;
}
.note-kind {
  color: var(--text-secondary);
}
.note-status {
  color: var(--success);
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
  white-space: pre-wrap;
}
</style>
