<script setup lang="ts">
import { computed } from 'vue';
const props = defineProps<{
  modelValue: string | null;
  inheritedPolicy?: string;
  global?: boolean;
  compact?: boolean;
}>();
const emit = defineEmits<{ 'update:modelValue': [value: string | null] }>();
const policies = [
  {
    value: 'directed',
    label: '设定需授权，状态自动保存',
    description: '剧情状态、MVU 变量和记忆正常保存。修改对话内角色设定或世界书，需勾选“本轮允许修改设定”。',
  },
  {
    value: 'evolving',
    label: '允许修改设定，自动保存',
    description:
      '允许 Agent 或已获脚本权限的角色卡修改对话内角色设定与世界书。状态、记忆和设定更新自动保存；允许修改不代表每轮都会修改。',
  },
  {
    value: 'preview',
    label: '状态与设定更新均需确认',
    description: '剧情状态、MVU 变量、记忆、摘要或设定有变化时，保留本轮草稿，等待你接受后再保存。',
  },
];
const effective = computed(
  () =>
    policies.find((policy) => policy.value === (props.modelValue || props.inheritedPolicy)) || policies[0],
);
</script>
<template>
  <div class="policy-control">
    <label
      >{{ global ? '默认对话更新策略' : '对话更新策略' }}
      <select
        :value="modelValue ?? ''"
        :aria-label="global ? '默认对话更新策略' : '对话更新策略'"
        @change="emit('update:modelValue', ($event.target as HTMLSelectElement).value || null)"
      >
        <option v-if="!global" value="">沿用全局默认</option>
        <option v-for="policy in policies" :key="policy.value" :value="policy.value">
          {{ policy.label }}
        </option>
      </select>
    </label>
    <div class="policy-help">
      <p v-if="!global && !modelValue" class="policy-effective">当前生效：{{ effective.label }}</p>
      <p>{{ effective.description }}</p>
      <dl v-if="!compact">
        <dt>影响对象</dt>
        <dd>剧情状态 / MVU 变量、记忆 / 摘要，以及对话内角色设定和世界书。</dd>
        <dt>作用层级</dt>
        <dd>
          {{
            global
              ? '作为未单独设置策略的对话的默认值。更新保存在各对话的剧情进度中。'
              : '仅当前对话的剧情进度；继承进度时一并复制。'
          }}
        </dd>
      </dl>
      <p class="policy-scope">对话内设定和世界书保存为对话快照，不会覆盖书架中的原角色卡。</p>
    </div>
  </div>
</template>
<style scoped>
.policy-help {
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.75;
  margin: 8px 0 16px;
}
.policy-help p {
  margin: 0 0 8px;
  font-size: inherit;
  line-height: inherit;
}
.policy-effective {
  color: var(--text-secondary);
}
.policy-help dl {
  display: grid;
  grid-template-columns: 4.5em minmax(0, 1fr);
  gap: 5px 8px;
  margin: 10px 0;
}
.policy-help dt {
  color: var(--text-secondary);
}
.policy-help dd {
  margin: 0;
}
.policy-help .policy-scope {
  padding-top: 8px;
  border-top: 1px solid var(--border-subtle);
}
</style>
