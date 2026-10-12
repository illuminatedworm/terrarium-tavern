<script setup lang="ts">
import { computed, inject, onBeforeUnmount, ref, watch } from 'vue';
import type { ContextOperations } from '../../composables/useContextOperations';
import { api } from '../../lib/api';

interface PlotSession {
  id: string;
  revision: number;
  head: string | null;
  plot_enabled?: boolean;
  snapshot: { full_agent?: { plot?: { text: string; revision: number } } };
}
const props = defineProps<{ session: PlotSession; disabled?: boolean; limit?: number }>();
const emit = defineEmits<{ saved: [session: PlotSession] }>();
const text = ref('');
const savedText = ref('');
const editRevision = ref(0);
const expanded = ref(false);
const saving = ref(false);
const contextOps = inject<ContextOperations>('context-operations');
const working = computed(() => props.disabled || saving.value || contextOps?.busy.value);
const error = ref('');
const conflict = ref(false);
const dirty = computed(() => text.value !== savedText.value);
let sessionId = '';
let generation = 0;
const currentText = (session: PlotSession) => session.snapshot.full_agent?.plot?.text || '';

watch(
  () => [props.session.id, props.session.revision],
  () => {
    if (sessionId !== props.session.id) {
      sessionId = props.session.id;
      generation++;
      text.value = savedText.value = currentText(props.session);
      editRevision.value = props.session.revision;
      expanded.value = saving.value = conflict.value = false;
      error.value = '';
    } else if (!dirty.value) {
      text.value = savedText.value = currentText(props.session);
      editRevision.value = props.session.revision;
      conflict.value = false;
    } else if (editRevision.value !== props.session.revision) {
      conflict.value = true;
      error.value = '会话已更新，未提交的规划仍已保留。请刷新并核对已保存版本后再保存。';
    }
  },
  { immediate: true },
);
onBeforeUnmount(() => generation++);

async function save(change: { enabled?: boolean; content?: string }) {
  if (working.value) return;
  const sid = props.session.id;
  const ticket = ++generation;
  saving.value = true;
  error.value = '';
  try {
    const updated: PlotSession = await api(
      `/sessions/${sid}/full-agent/plot`,
      {
        expected_revision: change.content === undefined ? props.session.revision : editRevision.value,
        ...change,
      },
      'PUT',
    );
    if (sid !== props.session.id || ticket !== generation) return;
    if (updated.revision < props.session.revision) {
      conflict.value = true;
      error.value = '保存后会话再次更新，编辑内容已保留。请刷新并核对最新版本。';
      return;
    }
    // A toggle can coexist with a local edit; never overwrite that edit.
    const canAdvance = editRevision.value === props.session.revision;
    if (change.content !== undefined) text.value = savedText.value = currentText(updated);
    if (canAdvance || change.content !== undefined) {
      editRevision.value = updated.revision;
      conflict.value = false;
    }
    emit('saved', updated);
  } catch (e: any) {
    if (sid === props.session.id && ticket === generation) {
      error.value = e.message;
      conflict.value = true;
    }
  } finally {
    if (sid === props.session.id && ticket === generation) saving.value = false;
  }
}

async function refresh() {
  if (working.value) return;
  const sid = props.session.id;
  const ticket = ++generation;
  saving.value = true;
  error.value = '';
  try {
    const updated: PlotSession = await api(`/sessions/${sid}`);
    if (sid !== props.session.id || ticket !== generation) return;
    if (updated.revision < props.session.revision) {
      error.value = '会话再次更新，请重新刷新；编辑内容已保留。';
      return;
    }
    savedText.value = currentText(updated);
    editRevision.value = updated.revision;
    conflict.value = true;
    error.value = '已刷新，编辑内容已保留。请核对下方已保存版本，再保存你的修改。';
    emit('saved', updated);
  } catch (e: any) {
    if (sid === props.session.id && ticket === generation) error.value = e.message;
  } finally {
    if (sid === props.session.id && ticket === generation) saving.value = false;
  }
}
</script>

<template>
  <section class="full-agent-plot" aria-label="剧情规划">
    <h3>剧情规划</h3>
    <label class="plot-toggle">
      <input
        type="checkbox"
        :checked="!!session.plot_enabled"
        :disabled="working"
        @change="save({ enabled: !session.plot_enabled })"
      />
      开启剧情规划
    </label>
    <p class="inspector-note">
      按当前对话保存可调整的阶段大纲，帮助铺设伏笔与后续转折。关闭后保留大纲并暂停使用。
    </p>
    <details :open="expanded" @toggle="expanded = ($event.target as HTMLDetailsElement).open">
      <summary>查看和编辑大纲（含未来剧情）</summary>
      <p class="inspector-note">编辑后模型仍可随剧情调整。明确的创作要求请通过对话或创作偏好传达。</p>
      <label>
        阶段大纲
        <textarea
          v-model="text"
          rows="10"
          :disabled="working || !session.head"
          placeholder="当前阶段、可能事件与转折、伏笔及推进条件…"
        />
      </label>
      <p v-if="!session.head" class="inspector-note">可以先开启功能；发送第一条消息后即可保存和编辑大纲。</p>
      <p v-else-if="!savedText" class="inspector-note">尚无大纲；开启后，模型会在下一次剧情回复前建立。</p>
      <p v-if="limit" class="inspector-note">大纲上限 {{ limit }} tokens。</p>
      <div class="plot-actions">
        <button :disabled="working || !session.head || !dirty" @click="save({ content: text })">
          {{ saving ? '正在保存…' : '保存大纲' }}
        </button>
        <button class="quiet" :disabled="working || !session.head || !text" @click="text = ''">
          清空编辑内容
        </button>
        <span v-if="dirty" role="status">有未保存修改</span>
      </div>
      <p class="inspector-note">保存空白大纲后，开启状态下的下一回合会重新建纲。</p>
      <details v-if="conflict">
        <summary>核对当前已保存版本</summary>
        <p class="plot-saved-text">{{ currentText(session) || '当前已保存大纲为空。' }}</p>
      </details>
    </details>
    <p v-if="error" role="alert">{{ error }}</p>
    <button v-if="conflict" class="quiet" :disabled="working" @click="refresh">
      刷新会话（保留编辑内容）
    </button>
  </section>
</template>

<style scoped>
.full-agent-plot {
  min-width: 0;
  display: grid;
  gap: 0.65rem;
}
.full-agent-plot h3 {
  margin: 0;
}
.plot-toggle,
.plot-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.5rem;
}
.full-agent-plot textarea {
  display: block;
  width: 100%;
  box-sizing: border-box;
  resize: vertical;
  max-height: 24rem;
}
.plot-saved-text {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 16rem;
  overflow: auto;
}
</style>
