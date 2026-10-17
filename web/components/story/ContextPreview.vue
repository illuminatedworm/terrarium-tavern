<script setup lang="ts">
import { computed } from 'vue';
import Icon from '../common/Icon.vue';
import ContextMeter from './ContextMeter.vue';
import ContextRows, { type ContextRowGroup } from './ContextRows.vue';
import { contextExcerpt, contextMessageContent } from '../../lib/contextPreview';

const props = defineProps<{
  preview: any;
  groups: Array<{
    id: string;
    source: string;
    kind: string;
    label: string;
    anchor: string;
    items: any[];
    tokens: number;
    count: number;
  }>;
  budgetSources: Array<{ label: string; kind: string; tokens: number; percent: number }>;
  contextTokens: number;
  actualInputLabel: string;
  lengthLabel: string;
  tokenizerLabel: string;
  cacheRate: string;
  itemTitle: (item: any) => string;
  /** Message id → 1-based round (0 for the opening before the first user turn). */
  rounds: Map<string, number>;
}>();

const budget = computed(() => Number(props.preview?.daily_context_budget || 0));
function roundLabel(item: any) {
  const round = item.message_id ? props.rounds.get(item.message_id) : undefined;
  return round == null ? '' : round ? `第 ${round} 轮` : '开场';
}
const rows = computed<ContextRowGroup[]>(() =>
  props.groups.map((group) => ({
    id: group.id,
    kind: group.kind,
    label: group.label,
    hint: group.source,
    meta: `${group.count} 条 · ${group.items.length} 块`,
    tokens: `${group.tokens.toLocaleString()} tokens`,
    anchored: group.kind === 'lore' && !!group.anchor,
    rows: group.items.map((item, index) => {
      const excerpt = contextExcerpt(item);
      return {
        key: item.position ?? index,
        round: roundLabel(item),
        title: props.itemTitle(item),
        tag: excerpt.tag,
        excerpt: excerpt.text,
        role: item.role,
        position: item.position,
        tokens: item.opaque
          ? '加密状态 · 占用未知'
          : `${Number(item.estimated_tokens || 0).toLocaleString()} tokens`,
        content: contextMessageContent(item),
      };
    }),
  })),
);
</script>

<template>
  <div class="context-preview">
    <h2 class="panel-title"><Icon name="layers" :size="19" />上下文预览</h2>
    <ContextMeter
      :scale="budget"
      :estimated="Number(preview?.estimated_tokens || 0)"
      :scale-label="`日常输入预算 ${budget.toLocaleString()} tokens`"
      :aside="actualInputLabel"
      :caption="lengthLabel"
      :sources="budgetSources"
      :stats="[
        { label: '模型上下文窗口', value: `${contextTokens.toLocaleString()} tokens` },
        { label: '内容块', value: String(preview?.trace?.length || 0) },
        { label: '缓存命中率', value: cacheRate },
      ]"
      :footnote="`${tokenizerLabel}预览不会加载神经模型，下方诊断会注明不完整的召回。`"
    />
    <ContextRows :groups="rows" />
    <details class="context-diagnostics">
      <summary>
        触发与裁剪
        <small
          >{{ preview?.diagnostics?.length || 0 }} 条诊断 ·
          {{ preview?.lore?.length || 0 }} 条世界书命中</small
        >
      </summary>
      <pre>{{
        JSON.stringify(
          { lore: preview?.lore, diagnostics: preview?.diagnostics, retrieval: preview?.retrieval },
          null,
          2,
        )
      }}</pre>
    </details>
  </div>
</template>
