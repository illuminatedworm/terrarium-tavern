<script setup lang="ts">
import { computed } from 'vue';

/**
 * A previous provider observation is never presented as the current context
 * size. The optional local tokenizer reference uses its own bar and
 * denominator; it only compares relative sizes and drives no mechanism.
 */
const props = defineProps<{
  scale: number;
  tokens: number | null;
  scaleLabel: string;
  aside?: string;
  caption: string;
  sources?: Array<{ label: string; kind: string; tokens: number; percent: number }>;
  /** Heading of the local reference bar, including its total and caveats. */
  sourcesLabel?: string;
  stats: Array<{ label: string; value: string }>;
  footnote?: string;
}>();

const usedPercent = computed(() =>
  props.scale && props.tokens != null ? Math.round((props.tokens / props.scale) * 100) : null,
);
// Widths reuse the legend's share of the request total, so framing the
// sources do not list stays as the unfilled remainder of the bar.
const segments = computed(() =>
  (props.sources ?? [])
    .filter((source) => source.tokens > 0)
    .map((source) => ({ ...source, width: Math.min(100, source.percent) })),
);
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
    <div v-if="usedPercent != null" class="context-meter-bar" aria-hidden="true">
      <span
        class="context-other"
        :style="{ width: `${Math.min(100, Math.max(0, usedPercent))}%`, minWidth: '0' }"
      ></span>
    </div>
    <template v-if="sources?.length">
      <div class="context-meter-head">
        <small>{{ sourcesLabel }}</small>
      </div>
      <div class="context-meter-bar" aria-hidden="true">
        <span
          v-for="segment in segments"
          :key="segment.label"
          :class="`context-${segment.kind}`"
          :style="{ width: `${segment.width}%` }"
          :title="`${segment.label} · ≈ ${segment.tokens.toLocaleString()} tokens`"
        ></span>
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
