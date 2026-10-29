<script setup lang="ts">
import { computed } from 'vue';

/**
 * A previous provider observation is never presented as the current context
 * size. The bar's filled length is that observation's share of `scale`; the
 * optional local tokenizer reference only splits the filled length by
 * relative size and drives no mechanism.
 */
const props = defineProps<{
  scale: number;
  tokens: number | null;
  scaleLabel: string;
  aside?: string;
  caption: string;
  sources?: Array<{ label: string; kind: string; tokens: number; percent: number }>;
  /** Heading of the local reference split, including its total and caveats. */
  sourcesLabel?: string;
  stats: Array<{ label: string; value: string }>;
  footnote?: string;
}>();

const usedPercent = computed(() =>
  props.scale && props.tokens != null ? Math.round((props.tokens / props.scale) * 100) : null,
);
// Each legend share (of the local total) scales the measured fill, so the
// segments together span exactly the measured share. Without a measurement the
// bar stays empty rather than passing the local reference off as usage.
const segments = computed(() => {
  if (usedPercent.value == null) return [];
  const filled = Math.min(100, Math.max(0, usedPercent.value));
  const listed = (props.sources ?? []).filter((source) => source.tokens > 0);
  if (!listed.length) return [{ label: '实测输入', kind: 'other', width: filled, title: '' }];
  return listed.map((source) => ({
    label: source.label,
    kind: source.kind,
    width: (filled * source.percent) / 100,
    title: `${source.label} · 本地 ≈ ${source.tokens.toLocaleString()} tokens`,
  }));
});
</script>

<template>
  <section class="context-meter" aria-label="上次实测输入">
    <div class="context-meter-head">
      <small>{{ scaleLabel }}</small
      ><small v-if="aside">{{ aside }}</small>
    </div>
    <div class="context-meter-figure" :class="{ over: (usedPercent ?? 0) > 100 }">
      <strong v-if="usedPercent != null">{{ usedPercent }}%</strong><span>{{ caption }}</span>
    </div>
    <div class="context-meter-bar" aria-hidden="true">
      <span
        v-for="segment in segments"
        :key="segment.label"
        :class="`context-${segment.kind}`"
        :style="{ width: `${segment.width}%` }"
        :title="segment.title || undefined"
      ></span>
    </div>
    <template v-if="sources?.length">
      <div class="context-meter-head">
        <small>{{ sourcesLabel }}</small>
      </div>
      <ul class="context-meter-legend" aria-label="本地分词预测分类占比">
        <li v-for="source in sources" :key="source.label" :class="`context-${source.kind}`">
          <i aria-hidden="true"></i>{{ source.label }} <b>{{ source.percent.toFixed(2) }}%</b>
        </li>
      </ul>
    </template>
    <dl class="context-stats">
      <div v-for="stat in stats" :key="stat.label">
        <dt>{{ stat.label }}</dt>
        <dd>{{ stat.value }}</dd>
      </div>
    </dl>
    <p v-if="footnote" class="context-footnote">{{ footnote }}</p>
  </section>
</template>
