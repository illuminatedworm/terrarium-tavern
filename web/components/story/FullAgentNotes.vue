<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { api } from '../../lib/api';
import FieldHelp from '../common/FieldHelp.vue';
import Icon from '../common/Icon.vue';
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
const handoffOpen = ref(false);
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
      <section class="scene-block" aria-label="窗口">
        <div class="scene-block-head">
          <h3>
            窗口<FieldHelp
              tooltip="Agent 自行维护交接笔记、查询历史并换窗。手动换窗复用已有交接，保留的剧情消息扩展到完整交流；退出范围须已有章节覆盖，交接正文和来源引用保持不变。长期笔记与完整剧情继续保留。"
            />
          </h3>
        </div>
        <form class="scene-block-body" @submit.prevent="newWindow">
          <div class="window-row">
            <span class="window-number">{{ session.snapshot.full_agent?.window?.number || 1 }}</span>
            <span class="window-label"><b>当前工作窗口</b>换窗后从交接笔记继续</span>
            <button type="submit" :disabled="!canSwitch">
              {{ switching ? '正在换窗…' : '换新窗口' }}
            </button>
          </div>
          <div class="keep-row">
            换窗时保留最近
            <span class="stepper">
              <button
                type="button"
                aria-label="减少保留条数"
                :disabled="disabled || switching || keepRecent <= 0"
                @click="keepRecent--"
              >
                <Icon name="minus" :size="13" /></button
              ><output aria-live="polite">{{ keepRecent }}</output
              ><button
                type="button"
                aria-label="增加保留条数"
                :disabled="disabled || switching || keepRecent >= 12"
                @click="keepRecent++"
              >
                <Icon name="plus" :size="13" />
              </button>
            </span>
            条剧情消息
          </div>
        </form>
      </section>
      <section class="scene-block" aria-label="交接笔记（handoff）">
        <div class="scene-block-head">
          <h3>
            交接笔记<span class="scene-block-tag">只读</span
            ><FieldHelp
              tooltip="这里显示模型实际使用的交接及来源标注。发现记忆错误时，请在对话中要求 Agent 核对并更新。"
            />
          </h3>
        </div>
        <div class="scene-block-body">
          <p v-if="loading && !records" role="status" class="state-empty">正在读取交接笔记…</p>
          <template v-else-if="records?.handoff.text">
            <p class="handoff-text" :class="{ open: handoffOpen }">{{ records.handoff.text }}</p>
            <button
              class="quiet handoff-toggle"
              :aria-expanded="handoffOpen"
              @click="handoffOpen = !handoffOpen"
            >
              {{ handoffOpen ? '收起' : '展开全文' }}
            </button>
          </template>
          <p v-else-if="records" class="state-empty">尚无交接笔记，Agent 会在需要时整理。</p>
        </div>
      </section>
    </template>
    <FullAgentPlot
      :session="session"
      :disabled="disabled || switching"
      :limit="records?.plot_tokens"
      @saved="emit('saved', $event)"
    />
    <section class="scene-block" aria-label="长期笔记">
      <div class="scene-block-head">
        <h3>
          长期笔记<span v-if="records" class="scene-block-count">{{ notes.length }}</span
          ><FieldHelp
            :tooltip="
              session.channel_origin === 'chatgpt'
                ? '两种上下文方式共用这些笔记，由 Agent 通过工具维护并按需读取。'
                : '这些笔记由 Agent 维护并按需读取，本地换窗不会删除长期笔记。'
            "
          />
        </h3>
        <button class="notes-filter" :aria-pressed="showArchived" @click="showArchived = !showArchived">
          含归档
        </button>
        <button
          class="icon-button"
          :title="loading ? '读取中…' : '刷新'"
          :aria-label="loading ? '读取中' : '刷新'"
          :disabled="loading || switching"
          @click="refreshNotes"
        >
          <Icon name="refresh" :size="16" />
        </button>
      </div>
      <div class="scene-block-body">
        <p v-if="error" role="alert" class="notes-error">{{ error }}</p>
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
      </div>
    </section>
  </section>
</template>

<style scoped>
.full-agent-notes {
  display: grid;
  gap: 12px;
  min-width: 0;
}
.window-row {
  display: flex;
  align-items: center;
  gap: 14px;
}
.window-number {
  color: var(--text-strong);
  font-family: var(--font-display);
  font-size: var(--fs-3xl);
  line-height: 1;
}
.window-label {
  flex: 1;
  min-width: 0;
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.5;
}
.window-label b {
  display: block;
  color: var(--text-secondary);
  font-size: var(--fs-sm);
  font-weight: 500;
}
.keep-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding-top: 10px;
  border-top: 1px solid var(--border-subtle);
  color: var(--text-secondary);
  font-size: var(--fs-sm);
}
.stepper {
  display: inline-flex;
  align-items: center;
  height: var(--control-h-sm);
  overflow: hidden;
  border: 1px solid var(--border-control);
  border-radius: var(--radius-xs);
  background: var(--surface-input);
}
.stepper button {
  display: grid;
  place-items: center;
  width: 26px;
  height: 100%;
  padding: 0;
  border: 0;
  border-radius: 0;
  background: none;
  color: var(--text-muted);
}
.stepper output {
  min-width: 22px;
  color: var(--text-strong);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  text-align: center;
}
/* Collapsed handoff shows its opening lines and fades out; the toggle reveals the rest. */
.handoff-text {
  max-height: 7.2em;
  margin: 0;
  overflow: hidden;
  padding: 2px 0 2px 12px;
  border-left: 2px solid var(--border-strong);
  color: var(--text-secondary);
  font-size: var(--fs-sm);
  line-height: 1.75;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  -webkit-mask-image: linear-gradient(#000 60%, transparent);
  mask-image: linear-gradient(#000 60%, transparent);
}
.handoff-text.open {
  max-height: none;
  -webkit-mask-image: none;
  mask-image: none;
}
.handoff-toggle {
  justify-self: end;
  padding: 2px 6px;
  font-size: var(--fs-xs);
}
.notes-filter {
  height: 24px;
  padding: 0 10px;
  border-radius: var(--radius-pill);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
}
.notes-filter[aria-pressed='true'] {
  border-color: rgb(var(--accent-rgb) / 0.4);
  background: var(--accent-soft);
  color: var(--text-strong);
}
.notes-error {
  margin: 0;
  color: var(--danger);
  font-size: var(--fs-sm);
}
.agent-note {
  --note-color: var(--ctx-memory);
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
.note-content {
  color: var(--text-primary);
  font-size: var(--fs-md);
  line-height: 1.8;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
