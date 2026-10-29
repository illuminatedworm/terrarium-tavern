<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { rawApi } from '../../lib/api';
import { contextExcerpt, contextMessageContent } from '../../lib/contextPreview';
import {
  contextTokens,
  contextUsageDetails,
  contextUsageLabel,
  contextUsageRows,
} from '../../lib/contextUsage';
import { localEstimateLabel, localEstimateNote, localTokensLabel } from '../../lib/localEstimate';
import AdvisorIcon from './AdvisorIcon.vue';
import ContextMeter from './ContextMeter.vue';
import ContextRows, { type ContextRow, type ContextRowGroup } from './ContextRows.vue';
const props = defineProps<{
  sessionId: string;
  /** Round of each story message in order; `H<n>` refs index into it. */
  storyRounds?: number[];
}>();
const emit = defineEmits<{ close: [] }>();
const dialog = ref<HTMLDialogElement>(),
  preview = ref<any>(null),
  error = ref('');
onMounted(async () => {
  dialog.value?.showModal();
  try {
    preview.value = await rawApi(`/sessions/${props.sessionId}/advisor/preview`);
  } catch (e) {
    error.value = String(e);
  }
});

const referenceFields: Record<string, string> = {
  name: '名称',
  description: '描述',
  personality: '性格',
  scenario: '场景',
  system_prompt: '系统提示',
  post_history_instructions: '后置指令',
  mes_example: '示例对话',
};
// Advisor messages carry no source field; the category follows the runtime
// message type and the wrapper tag the backend writes (advisor/context.py).
function classify(message: any, meta: any, tag: string, attrs: Record<string, string>) {
  if (message._native_response) return { key: 'cloud', kind: 'cloud', label: '云端压缩状态' };
  if (meta?.message_type === 'root_system') return { key: 'rules', kind: 'rules', label: '参谋规则' };
  if (meta?.message_type === 'advisor_notice') return { key: 'notice', kind: 'turn', label: '旧咨询提示' };
  if (tag === 'story_reference')
    return {
      key: 'card',
      kind: 'character',
      label: '角色卡资料',
      title: referenceFields[attrs.field] || attrs.field,
    };
  if (tag === 'user_character') return { key: 'persona', kind: 'persona', label: '用户角色' };
  if (tag === 'lorebook')
    return meta?.runtime_phase === 'body'
      ? { key: 'lore_story', kind: 'lore', label: '剧情锚定世界书', anchored: true }
      : { key: 'lore_constant', kind: 'lore', label: '常驻世界书' };
  if (tag === 'turn_preference') return { key: 'preference', kind: 'session', label: '本轮偏好' };
  if (tag === 'handoff')
    return {
      key: 'handoff',
      kind: 'memory',
      label: '交接笔记',
      title: attrs.stale === 'True' ? '需核对' : '',
    };
  if (tag === 'story_memory') return { key: 'memory', kind: 'memory', label: '故事记忆' };
  if (tag === 'consultation_summary') return { key: 'summary', kind: 'memory', label: '旧咨询摘要' };
  if (tag === 'story_history') {
    const round = props.storyRounds?.[Number(attrs.ref?.slice(1)) - 1];
    return {
      key: 'history',
      kind: 'history',
      label: '剧情原文',
      title: `${attrs.ref} · ${attrs.role === 'user' ? '用户' : '角色'}`,
      round: round == null ? '' : round ? `第 ${round} 轮` : '开场',
    };
  }
  if (message.role === 'tool')
    return { key: 'consult', kind: 'session', label: '咨询记录', title: '工具返回' };
  if (message.role === 'assistant')
    return {
      key: 'consult',
      kind: 'session',
      label: '咨询记录',
      title: message.tool_calls?.length ? '工具调用' : '参谋 agent',
    };
  if (message.role === 'user')
    return { key: 'consult', kind: 'session', label: '咨询记录', title: '用户提问' };
  return { key: 'other', kind: 'other', label: '其他' };
}
// Rows follow `messages`; the preview-only local reference sits on the
// matching `trace` row (same order), never on the request message itself.
const categorized = computed(() =>
  ((preview.value?.messages || []) as any[]).map((message: any, index: number) => {
    const excerpt = contextExcerpt(message);
    return {
      message,
      index,
      excerpt,
      local: preview.value.trace?.[index],
      category: classify(message, preview.value.runtime_trace?.[index], excerpt.tag, excerpt.attrs),
    };
  }),
);
const groups = computed<ContextRowGroup[]>(() => {
  const result: ContextRowGroup[] = [];
  const sums = new Map<ContextRowGroup, number>();
  categorized.value.forEach(({ message, index, excerpt, local, category }) => {
    const row: ContextRow = {
      key: index,
      round: category.round,
      title: category.title,
      tag: excerpt.tag,
      excerpt: message._native_response ? '加密内容由服务端解释' : excerpt.text,
      role: message.role,
      position: index,
      tokens: localTokensLabel(local),
      content: message._native_response
        ? '云端压缩状态（加密内容由服务端解释）'
        : contextMessageContent(message),
    };
    let group = result.at(-1);
    if (group?.id.startsWith(`${category.key}:`)) group.rows.push(row);
    else {
      group = {
        id: `${category.key}:${result.length}`,
        kind: category.kind,
        label: category.label,
        anchored: category.anchored,
        rows: [row],
      };
      result.push(group);
    }
    sums.set(group, (sums.get(group) ?? 0) + Number(local?.local_tokens || 0));
  });
  for (const group of result) {
    if (group.rows.length > 1) group.meta = `${group.rows.length} 块`;
    if (preview.value?.local_estimate) group.tokens = `≈ ${sums.get(group)!.toLocaleString()} tokens`;
  }
  return result;
});
const sources = computed(() => {
  const estimate = preview.value?.local_estimate;
  if (!estimate) return [];
  const totals = new Map<string, { kind: string; tokens: number }>();
  const add = (label: string, kind: string, tokens: number) => {
    const entry = totals.get(label) || { kind, tokens: 0 };
    entry.tokens += tokens;
    totals.set(label, entry);
  };
  for (const { local, category } of categorized.value)
    if (local?.local_tokens != null) add(category.label, category.kind, local.local_tokens);
  if (estimate.tool_definitions) add('工具定义', 'tool', estimate.tool_definitions);
  return Array.from(totals, ([label, { kind, tokens }]) => ({
    label,
    kind,
    tokens,
    percent: Math.round((tokens / Math.max(1, estimate.tokens)) * 10000) / 100,
  }));
});
const footnote = computed(() => {
  const p = preview.value;
  return [
    '下次咨询的背景预览，不含尚未发送的问题；运行中的咨询使用开始时冻结的剧情。',
    contextUsageDetails(p, { rows: false }),
    localEstimateNote(p.local_estimate),
    p.opaque ? '包含参谋 agent 独立的云端压缩状态；压缩后的占用等待新实测。' : '',
    p.summary ? '已包含旧咨询摘要。摘要不属于故事记忆。' : '',
  ].join('');
});
</script>
<template>
  <Teleport to="body">
    <dialog
      ref="dialog"
      class="advisor-preview"
      aria-label="参谋 agent 上下文"
      @cancel.prevent="emit('close')"
      @click="$event.target === dialog && emit('close')"
    >
      <header>
        <h2><AdvisorIcon name="compass" />参谋 agent 上下文</h2>
        <button aria-label="关闭参谋 agent 上下文" title="关闭" @click="emit('close')">
          <AdvisorIcon name="close" />
        </button>
      </header>
      <div class="advisor-preview-body">
        <p v-if="error" role="alert">{{ error }}</p>
        <p v-else-if="!preview" role="status">正在编译预览…</p>
        <template v-else>
          <ContextMeter
            :scale="preview.context_limit"
            :tokens="contextTokens(preview)"
            :scale-label="`参谋 agent 上下文窗口 ${preview.context_limit.toLocaleString()} tokens`"
            aside="只读预览"
            :caption="`${contextUsageLabel(preview)}${contextTokens(preview) == null ? '' : ` ${contextTokens(preview)!.toLocaleString()} tokens`}`"
            :sources="sources"
            :sources-label="localEstimateLabel(preview.local_estimate)"
            :stats="[
              { label: '剧情原文', value: `${preview.recent_refs.length} 条` },
              { label: '内容块', value: String(preview.messages.length) },
              { label: '可用工具', value: String(preview.tools.length) },
              ...contextUsageRows(preview),
            ]"
            :footnote="footnote"
          />
          <ContextRows :groups="groups" />
          <details class="context-diagnostics">
            <summary>
              可用工具 <small>{{ preview.tools.length }} 个</small>
            </summary>
            <pre>{{ JSON.stringify(preview.tools, null, 2) }}</pre>
          </details>
        </template>
      </div>
    </dialog>
  </Teleport>
</template>
<style scoped>
/* Native modal dialog (focus and background isolation) dressed as the app's float layer. */
.advisor-preview {
  width: min(1000px, calc(100vw - 32px));
  height: min(820px, calc(100dvh - 32px));
  max-width: none;
  max-height: none;
  padding: 0;
  border: 0;
  border-radius: var(--radius-xl);
  background: var(--glass-sheen), var(--glass-float);
  -webkit-backdrop-filter: var(--glass-blur-strong);
  backdrop-filter: var(--glass-blur-strong);
  box-shadow:
    var(--glass-highlight),
    inset 0 0 0 1px var(--glass-edge-strong),
    var(--shadow-float);
  color: var(--text-primary);
  overflow: hidden;
}
.advisor-preview::backdrop {
  background: rgb(var(--canvas-rgb) / 0.6);
  -webkit-backdrop-filter: blur(5px) saturate(120%);
  backdrop-filter: blur(5px) saturate(120%);
}
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 70px;
  padding: 0 20px 0 32px;
  box-sizing: border-box;
}
h2 {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0;
  font-size: var(--fs-title);
}
h2 :deep(.icon) {
  color: var(--text-secondary);
}
header button {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  padding: 0;
  border: 1px solid var(--glass-edge);
  border-radius: 50%;
  background: rgb(255 255 255 / 0.05);
  color: var(--text-secondary);
}
header button:hover {
  background: rgb(255 255 255 / 0.12);
  color: var(--text-primary);
}
.advisor-preview-body {
  height: calc(100% - 70px);
  overflow: auto;
  padding: 4px 32px 32px;
  box-sizing: border-box;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
}
@media (max-width: 760px) {
  header {
    padding-left: 16px;
  }
  .advisor-preview-body {
    padding-inline: 16px;
  }
}
</style>
