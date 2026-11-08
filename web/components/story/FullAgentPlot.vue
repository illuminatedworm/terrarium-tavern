<script setup lang="ts">
import { computed, inject, onBeforeUnmount, ref, watch } from 'vue';
import type { ContextOperations } from '../../composables/useContextOperations';
import { api } from '../../lib/api';
import FieldHelp from '../common/FieldHelp.vue';

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
  <section class="scene-block full-agent-plot" aria-label="剧情规划">
    <div class="scene-block-head">
      <h3>
        剧情规划<FieldHelp
          tooltip="开启后由模型主导局势：你决定角色言行，结果由模型裁决；大纲中的锚点不因故事内言行改变，只能用场外指令调整。关闭后保留大纲并暂停使用。"
        />
      </h3>
      <input
        type="checkbox"
        class="switch"
        role="switch"
        aria-label="开启剧情规划"
        :checked="!!session.plot_enabled"
        :disabled="working"
        @change="save({ enabled: !session.plot_enabled })"
      />
    </div>
    <div class="scene-block-body">
      <div class="plot-summary">
        <span>{{ session.plot_enabled ? '已开启，模型按大纲推进' : '已暂停，大纲保留' }}</span>
        <button :aria-expanded="expanded" @click="expanded = !expanded">
          {{ expanded ? '收起大纲' : '查看和编辑大纲' }}
        </button>
      </div>
      <template v-if="expanded">
        <label>
          <span
            >阶段大纲（含未来剧情）<FieldHelp
              tooltip="你写下或改动的锚点模型会原样保留，其余内容模型仍可随剧情调整。保存空白大纲后，开启状态下的下一回合会重新建纲。"
          /></span>
          <textarea
            v-model="text"
            rows="10"
            :disabled="working || !session.head"
            placeholder="核心、锚点、各方动向、人物变化、当前阶段、后续方向、伏笔…"
          />
        </label>
        <p v-if="!session.head" class="plot-note">可以先开启功能；发送第一条消息后即可保存和编辑大纲。</p>
        <p v-else-if="!savedText" class="plot-note">尚无大纲；开启后，模型会在下一次剧情回复前建立。</p>
        <p v-if="limit" class="plot-note">大纲上限 {{ limit }} tokens。</p>
        <div class="plot-actions">
          <button :disabled="working || !session.head || !dirty" @click="save({ content: text })">
            {{ saving ? '正在保存…' : '保存大纲' }}
          </button>
          <button class="quiet" :disabled="working || !session.head || !text" @click="text = ''">
            清空编辑内容
          </button>
          <span v-if="dirty" role="status">有未保存修改</span>
        </div>
        <details v-if="conflict">
          <summary>核对当前已保存版本</summary>
          <p class="plot-saved-text">{{ currentText(session) || '当前已保存大纲为空。' }}</p>
        </details>
      </template>
      <p v-if="error" role="alert" class="plot-error">{{ error }}</p>
      <button v-if="conflict" class="quiet" :disabled="working" @click="refresh">
        刷新会话（保留编辑内容）
      </button>
    </div>
  </section>
</template>

<style scoped>
.full-agent-plot h3 {
  margin: 0;
}
.plot-summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  color: var(--text-muted);
  font-size: var(--fs-xs);
}
.plot-summary button {
  flex: none;
  padding: 5px 10px;
  font-size: var(--fs-xs);
}
.full-agent-plot label {
  margin: 0;
  font-size: var(--fs-sm);
}
.full-agent-plot label > span {
  display: inline-flex;
  align-items: center;
}
.plot-note {
  margin: 0;
  color: var(--text-muted);
  font-size: var(--fs-xs);
}
.plot-error {
  margin: 0;
  color: var(--danger);
  font-size: var(--fs-sm);
}
.plot-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.5rem;
}
.plot-actions span {
  color: var(--warning);
  font-size: var(--fs-xs);
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
