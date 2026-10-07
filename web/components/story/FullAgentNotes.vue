<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { api } from '../../lib/api';

const props = defineProps<{ session: any; disabled?: boolean }>();
const emit = defineEmits<{ saved: [session: any] }>();
const handoff = ref('');
const records = ref<any>(null);
const saving = ref(false);
const error = ref('');
const showArchived = ref(false);
const notes = computed(() =>
  (records.value?.notes || []).filter((n: any) => showArchived.value || n.status === 'active'),
);
watch(
  () => [props.session.id, props.session.revision],
  async () => {
    const sid = props.session.id;
    handoff.value = props.session.snapshot.full_agent?.handoff?.text || '';
    records.value = null;
    error.value = '';
    try {
      const result = await api(`/sessions/${sid}/full-agent`);
      if (props.session.id === sid) records.value = result;
    } catch (e: any) {
      if (props.session.id === sid) error.value = e.message;
    }
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
        placeholder="当前场景、用户约束、未决线索与下一步…"
      />
    </label>
    <p v-if="records?.handoff?.stale" role="status">原始剧情已编辑，这份交接笔记需要核对。</p>
    <div class="handoff-actions">
      <button :disabled="disabled || saving || !handoff.trim()" @click="save(false)">保存交接笔记</button>
      <button :disabled="disabled || saving || !handoff.trim()" @click="save(true)">保存并换新窗口</button>
    </div>
    <p class="inspector-note">
      手动换窗后仅携带交接笔记与基础设定。全部原文和工具记录仍可搜索，下一条发言会进入新窗口。
    </p>
    <p v-if="error" role="alert">{{ error }}</p>
    <h3>按需笔记</h3>
    <p class="inspector-note">
      这些笔记由 Agent 通过工具维护，按需读取；从第二个窗口起自动提供有效笔记的名称列表。
    </p>
    <label><input v-model="showArchived" type="checkbox" />显示已归档笔记</label>
    <details v-for="note in notes" :key="note.key" class="agent-note">
      <summary>
        {{ note.key }} · {{ kindLabel[note.kind] }}{{ note.status === 'archived' ? ' · 已归档' : '' }}
      </summary>
      <p v-if="note.stale" role="status">来源已编辑或不在当前分支，请核对。</p>
      <p class="note-content">{{ note.content }}</p>
      <small v-if="note.sources.length">原文来源：{{ note.sources.join('、') }}</small>
    </details>
    <p v-if="!notes.length" class="state-empty">暂无笔记。需要记住某件事时，可以直接告诉 Agent。</p>
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
  margin-block: 12px;
  overflow-wrap: anywhere;
}
.agent-note summary {
  cursor: pointer;
}
.note-content {
  white-space: pre-wrap;
}
</style>
