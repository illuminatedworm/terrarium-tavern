<script setup lang="ts">
import { computed, ref, watch } from 'vue';
const props = defineProps<{
  session: any;
  breakpointId: string | null;
  requestState?: any;
  sentLabel: string;
  disabled: boolean;
  retention: { value: string; enabled: boolean; note: string; options: { value: string; label: string }[] };
  runtime: boolean;
}>();
const emit = defineEmits<{
  save: [automatic: boolean, messageId: string | null, interval: number, offset: number];
  locate: [];
  'save-retention': [value: string];
}>();
const automatic = ref(true);
const interval = ref<number | string>(5);
const offset = ref<number | string>(3);
const retentionValue = ref('');
watch(
  () => [props.session.id, props.retention.value],
  () => {
    retentionValue.value = props.retention.value;
  },
  { immediate: true },
);
watch(
  () => [props.session.id, props.session.revision],
  () => {
    automatic.value = props.session.cache_breakpoint_auto;
    interval.value = props.session.cache_breakpoint_follow?.interval || 5;
    offset.value = props.session.cache_breakpoint_follow?.offset || 3;
  },
  { immediate: true },
);
const valid = computed(() =>
  [interval.value, offset.value].every((value) => Number.isSafeInteger(Number(value)) && Number(value) >= 1),
);
const changed = computed(
  () =>
    automatic.value !== props.session.cache_breakpoint_auto ||
    Number(interval.value) !== props.session.cache_breakpoint_follow?.interval ||
    Number(offset.value) !== props.session.cache_breakpoint_follow?.offset,
);
const plannedLabel = computed(() => {
  const index = props.session.messages.findIndex((message: any) => message.id === props.breakpointId);
  return index < 0 ? '固定前缀' : `历史 H${index + 1} · AI 回复末尾`;
});
function save() {
  if (!valid.value || props.disabled) return;
  emit('save', automatic.value, props.breakpointId, Number(interval.value), Number(offset.value));
}
</script>

<template>
  <section class="cache-panel" aria-label="缓存断点控制">
    <h2>缓存时效</h2>
    <form v-if="retention.options.length" @submit.prevent="emit('save-retention', retentionValue)">
      <label
        >缓存时效档位<select
          v-model="retentionValue"
          aria-label="缓存时效档位"
          :disabled="disabled || !retention.enabled"
        >
          <option v-for="option in retention.options" :key="option.value" :value="option.value">
            {{ option.label }}
          </option>
        </select></label
      >
      <p class="inspector-note">{{ retention.note }}</p>
      <p class="inspector-note">应用于当前模式的正文模型及其所有会话，从下一次请求生效。</p>
      <button
        class="primary"
        :disabled="disabled || !retention.enabled || retentionValue === retention.value"
      >
        保存缓存时效
      </button>
    </form>
    <p v-else class="inspector-note">{{ retention.note }}</p>
    <template v-if="runtime">
      <h2>缓存断点</h2>
      <p class="inspector-note">选择稳定前缀的结束位置。悬停 AI 回复末尾，点击黄点可手动固定。</p>
      <form @submit.prevent="save">
        <label
          >跟进方式<select v-model="automatic" :disabled="disabled" aria-label="缓存跟进方式">
            <option :value="true">自动跟进</option>
            <option :value="false">手动固定</option>
          </select></label
        >
        <div class="cache-follow-fields">
          <label
            >每新增几轮跟进<input
              v-model="interval"
              type="number"
              min="1"
              step="1"
              required
              :disabled="disabled || !automatic"
          /></label>
          <label
            >倒数第几个 AI 回复<input
              v-model="offset"
              type="number"
              min="1"
              step="1"
              required
              :disabled="disabled || !automatic"
          /></label>
        </div>
        <p v-if="!valid" class="error" role="alert">请输入大于等于 1 的整数。</p>
        <p v-if="automatic" class="inspector-note">
          保存后立即定位，再按新增的已完成回复计数；重新生成不计轮次。
        </p>
        <button class="primary" :disabled="disabled || !valid || !changed">保存跟进策略</button>
      </form>
      <div class="cache-plan-state">
        <small>计划断点</small><strong>{{ plannedLabel }}</strong>
        <p v-if="session.cache_breakpoint_reason">{{ session.cache_breakpoint_reason }}</p>
        <p v-if="session.cache_breakpoint_auto">
          再新增
          {{ session.cache_breakpoint_follow.interval - session.cache_breakpoint_follow.progress }} 轮后跟进
        </p>
        <p v-else>位置已手动固定</p>
        <button @click="emit('locate')">定位断点</button>
      </div>
      <div class="cache-plan-state" role="status">
        <small>上次请求</small>
        <template v-if="requestState?.status === 'fallback'"
          ><strong>已降级云端</strong>
          <p>{{ requestState.reason }}</p></template
        >
        <template v-else-if="requestState?.status === 'sent'"
          ><strong>{{ sentLabel }}</strong>
          <p>断点已发送，不代表缓存已命中。</p></template
        >
        <p v-else>尚无本地缓存断点发送记录。</p>
      </div>
    </template>
  </section>
</template>

<style scoped>
.cache-panel {
  padding: 0 2px 24px;
}
form,
form label {
  display: grid;
  gap: 10px;
}
form {
  gap: 16px;
}
.cache-follow-fields {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
input,
select {
  width: 100%;
  min-width: 0;
}
.cache-plan-state {
  border-top: 1px solid var(--border-subtle);
  margin-top: 24px;
  padding-top: 18px;
  font-size: 12px;
  line-height: 1.7;
}
.cache-plan-state small,
.cache-plan-state strong {
  display: block;
}
.cache-plan-state small {
  color: var(--text-muted);
  margin-bottom: 6px;
}
.cache-plan-state p {
  color: var(--text-secondary);
}
</style>
