<script setup lang="ts">
import { computed } from 'vue';

/** Usage summary shared by the context previews: share of `scale`, split by budget source. */
const props = defineProps<{
  scale: number;
  estimated: number;
  scaleLabel: string;
  aside?: string;
  caption: string;
  sources: Array<{ label: string; kind: string; tokens: number; percent: number }>;
  stats: Array<{ label: string; value: string }>;
  footnote?: string;
}>();

const usedPercent = computed(() => (props.scale ? Math.round((props.estimated / props.scale) * 100) : null));
// Segments share the meter scale, so the filled length equals the used share.
const segments = computed(() =>
  props.sources
    .filter((source) => source.tokens > 0)
    .map((source) => ({ ...source, width: props.scale ? (source.tokens / props.scale) * 100 : 0 })),
);
</script>

<template>
  <section class="context-meter" aria-label="上下文占用">
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
        :title="`${segment.label} · ${segment.tokens.toLocaleString()} tokens`"
      ></span>
    </div>
    <ul class="context-meter-legend" aria-label="输入占比">
      <li v-for="source in sources" :key="source.label" :class="`context-${source.kind}`">
        <i aria-hidden="true"></i>{{ source.label }} <b>{{ source.percent.toFixed(2) }}%</b>
      </li>
    </ul>
    <dl class="context-stats">
      <div v-for="stat in stats" :key="stat.label">
        <dt>{{ stat.label }}</dt>
        <dd>{{ stat.value }}</dd>
      </div>
    </dl>
    <p v-if="footnote" class="context-footnote">{{ footnote }}</p>
  </section>
</template>
