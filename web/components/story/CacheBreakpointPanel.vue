<script setup lang="ts">
import { computed, ref, watch } from 'vue';
const props = defineProps<{
  session: any;
  breakpointId: string | null;
  requestState?: any;
  sentLabel: string;
  disabled: boolean;
  retention: {
    model: string;
    apiLabel: string;
    protocolNote: string;
    value: string;
    enabled: boolean;
    note: string;
    options: { value: string; label: string }[];
  };
  runtime: boolean;
}>();
const emit = defineEmits<{
  save: [automatic: boolean, messageId: string | null, interval: number, offset: number];
  locate: [];
  'save-retention': [value: string];
}>();
const automatic = ref(true);
const interval = ref<number | string>(2);
const offset = ref<number | string>(2);
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
    interval.value = props.session.cache_breakpoint_follow?.interval || 2;
    offset.value = props.session.cache_breakpoint_follow?.offset || 2;
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
    <div class="cache-connection" aria-label="当前缓存接入">
      <p>当前模型：{{ retention.model || '尚未配置' }}</p>
      <p>接入方式：{{ retention.apiLabel }}</p>
      <p v-if="retention.protocolNote">{{ retention.protocolNote }}</p>
    </div>
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
        <p class="inspector-note">
          将断点设在倒数第 1 个 AI
          回复末尾属于激进策略：最新回复会进入缓存前缀，编辑或重新生成它可能使缓存失效，并触发重新写入。
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
.cache-connection {
  margin-bottom: 18px;
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}
.cache-connection p {
  margin: 4px 0;
}
.cache-panel h2 {
  margin: 0 0 12px;
  font-size: 16px;
  line-height: 1.4;
}
.cache-panel form + h2,
.cache-panel > .inspector-note + h2 {
  margin-top: 24px;
  padding-top: 18px;
  border-top: 1px solid var(--border-subtle);
}
form {
  display: grid;
  gap: 10px;
}
form label {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 12px;
  margin: 0;
  font-size: 12.5px;
  line-height: 1.5;
}
.cache-follow-fields {
  display: grid;
  gap: 8px;
}
input,
select {
  min-width: 0;
  max-width: 100%;
  min-height: 34px;
  margin: 0;
  padding: 6px 10px;
  font-size: 12.5px;
  line-height: 1.5;
}
select {
  width: 140px;
}
input {
  width: 76px;
  font-variant-numeric: tabular-nums;
}
.cache-panel .inspector-note,
.cache-panel .error {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
}
.cache-panel > h2 + .inspector-note {
  margin-bottom: 12px;
}
.cache-panel button {
  width: fit-content;
  min-height: 34px;
  padding: 6px 12px;
  font-size: 12px;
  line-height: 1.5;
}
form > button {
  justify-self: end;
  margin-top: 2px;
}
.cache-plan-state {
  border-top: 1px solid var(--border-subtle);
  margin-top: 18px;
  padding-top: 14px;
  font-size: 12px;
  line-height: 1.6;
}
.cache-plan-state small,
.cache-plan-state strong {
  display: block;
}
.cache-plan-state small {
  color: var(--text-muted);
  margin-bottom: 4px;
  font-size: 12px;
}
.cache-plan-state strong {
  font-size: 13px;
  font-weight: 500;
}
.cache-plan-state p {
  color: var(--text-secondary);
  margin: 4px 0 0;
}
.cache-plan-state button {
  margin-top: 10px;
}
</style>
