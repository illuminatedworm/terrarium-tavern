<script setup lang="ts">
import { computed, reactive, watch } from 'vue';
import type { EditorCacheBreakpoint, EditorCacheFollow, EditorMessage } from '../../lib/cardAuthor';

const props = defineProps<{
  policy: EditorCacheFollow;
  plan?: EditorCacheBreakpoint;
  messages: EditorMessage[];
  locked: boolean;
  error: string;
  statsError: string;
}>();
const emit = defineEmits<{ save: [policy: EditorCacheFollow] }>();
const draft = reactive({ ...props.policy });
const replies = computed(() => props.messages.filter((message) => message.role === 'assistant'));
const fields = ['mode', 'interval', 'offset', 'reply_ref'] as const;
const maximum = 2 ** 31 - 1;
watch(
  fields.map((key) => () => props.policy[key]),
  () => Object.assign(draft, props.policy),
);
const selection = computed<EditorCacheFollow>(() => ({
  mode: draft.mode,
  interval: draft.mode === 'auto' ? draft.interval : props.policy.interval,
  offset: draft.mode === 'auto' ? draft.offset : props.policy.offset,
  reply_ref: draft.mode === 'manual' ? draft.reply_ref : props.policy.reply_ref,
}));
const changed = computed(() => fields.some((key) => selection.value[key] !== props.policy[key]));
const valid = computed(
  () =>
    [selection.value.interval, selection.value.offset].every(
      (value) => Number.isInteger(value) && value >= 1 && value <= maximum,
    ) &&
    (draft.mode !== 'manual' || !!draft.reply_ref),
);
function save() {
  if (valid.value && changed.value && !props.locked) emit('save', selection.value);
}
</script>

<template>
  <section aria-label="制卡缓存策略">
    <p v-if="error" class="cw-notice danger" role="alert">{{ error }}</p>
    <p v-if="!plan" class="muted">{{ statsError || '正在读取当前缓存方式…' }}</p>
    <p v-else-if="!plan.enabled" class="muted">{{ plan.reason }}</p>
    <template v-else>
      <p class="cw-cache-plan" role="status">
        计划断点 · <strong>{{ plan.reply_ref ? `${plan.reply_ref} 回合末尾` : '固定前缀' }}</strong>
      </p>
      <p v-if="plan.reason" class="muted">{{ plan.reason }}</p>
      <p v-if="plan.remaining != null" class="muted">再完成 {{ plan.remaining }} 轮后跟进</p>
      <form @submit.prevent="save">
        <label>
          断点策略
          <select v-model="draft.mode" :disabled="locked">
            <option value="fixed">固定前缀</option>
            <option value="auto">自动跟进</option>
            <option value="manual" :disabled="!replies.length">手动固定</option>
          </select>
        </label>
        <template v-if="draft.mode === 'auto'">
          <label>
            每新增几轮跟进一次
            <input
              v-model.number="draft.interval"
              type="number"
              min="1"
              :max="maximum"
              required
              :disabled="locked"
            />
          </label>
          <label>
            倒数第几个已完成回复
            <input
              v-model.number="draft.offset"
              type="number"
              min="1"
              :max="maximum"
              required
              :disabled="locked"
            />
          </label>
          <p class="muted">
            从发送要求到最终回复成功提交算 1 轮。同轮的工具调用和模型续答不单独计数。保存后立即重新定位。
          </p>
        </template>
        <label v-else-if="draft.mode === 'manual'">
          固定在已完成回合末尾
          <select v-model="draft.reply_ref" required :disabled="locked">
            <option :value="null" disabled>请选择回复</option>
            <option v-for="reply in replies" :key="reply.ref" :value="reply.ref">
              {{ reply.ref }} · {{ reply.content.slice(0, 48) }}
            </option>
          </select>
        </label>
        <button type="submit" class="primary" :disabled="locked || !valid || !changed">保存策略</button>
      </form>
      <p class="muted">
        保存到当前对话，下次请求生效。计划断点只表示本次选择的位置，服务端仍可复用有效的旧断点。
      </p>
    </template>
  </section>
</template>

<style scoped>
.cw-cache-plan {
  margin-top: 0;
}
form {
  padding: 12px 0;
  border-block: 1px solid var(--border-subtle);
}
label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 12px;
  color: var(--text-secondary);
  font-size: var(--fs-sm);
}
select,
input {
  width: 100%;
  min-width: 0;
}
.muted {
  font-size: var(--fs-sm);
}
</style>
