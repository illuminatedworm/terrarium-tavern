<script setup lang="ts">
import { computed, useId } from 'vue';
import { useAnchoredTip } from '../../composables/useAnchoredTip';
import {
  conversationCacheRate,
  conversationCacheRatio,
  type ConversationCacheUsage,
} from '../../lib/editorContextStats';

const props = defineProps<{ usage?: ConversationCacheUsage | null }>();
const { anchor, bubble, open, position, listeners } = useAnchoredTip();
const tipId = useId();
const rate = computed(() => conversationCacheRate(props.usage));
const ratio = computed(() => conversationCacheRatio(props.usage));
const count = (value: number | null | undefined) => (value == null ? '—' : value.toLocaleString());
const rows = computed(() => {
  const usage = props.usage;
  const missed =
    usage?.prompt_tokens == null || usage.cached_tokens == null
      ? null
      : usage.prompt_tokens - usage.cached_tokens;
  return [
    { label: '缓存读取', value: count(usage?.cached_tokens), kind: 'hit' },
    { label: '未命中', value: count(missed), kind: 'miss' },
    { label: '输入合计', value: count(usage?.prompt_tokens), kind: 'total' },
  ];
});
</script>

<template>
  <span ref="anchor" class="cache-rate-stat" tabindex="0" :aria-describedby="tipId" v-on="listeners"
    >累计缓存命中率 <strong>{{ rate }}</strong></span
  >
  <Teleport to="body">
    <div
      v-if="open"
      :id="tipId"
      ref="bubble"
      class="field-tooltip cache-rate-tip"
      role="tooltip"
      :style="position"
    >
      <header>
        <span>累计缓存命中率</span>
        <strong>{{ rate }}</strong>
      </header>
      <div class="cache-rate-bar">
        <i :style="{ width: `${(ratio ?? 0) * 100}%` }"></i>
      </div>
      <dl>
        <div v-for="row in rows" :key="row.label" :class="row.kind">
          <dt>{{ row.label }}</dt>
          <dd>{{ row.value }}</dd>
        </div>
      </dl>
      <p>
        单位 tokens，按本会话全部请求的接口实际用量累计。运行期间保留上次完整统计，结束后刷新{{
          ratio == null ? '；尚无完整计数时显示“暂无”' : ''
        }}。
      </p>
    </div>
  </Teleport>
</template>
