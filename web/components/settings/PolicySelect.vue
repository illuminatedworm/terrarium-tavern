<script setup lang="ts">
import { computed } from 'vue';
import { policies } from '../../lib/settingsFields';
const props = defineProps<{
  modelValue: string | null;
  inheritedPolicy?: string;
  global?: boolean;
  compact?: boolean;
}>();
const emit = defineEmits<{ 'update:modelValue': [value: string | null] }>();
const effective = computed(
  () =>
    policies.find((policy) => policy.value === (props.modelValue || props.inheritedPolicy)) || policies[0],
);
</script>
<template>
  <div class="policy-control">
    <label
      >{{ global ? '默认修改保存方式' : '修改保存方式' }}
      <select
        :value="modelValue ?? ''"
        :aria-label="global ? '默认修改保存方式' : '修改保存方式'"
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
      <p>本轮开关决定是否更新变量、允许修改角色设定与世界书。保存方式不授予修改权限。</p>
      <p>记忆与笔记自动随回合保存，不单独要求审核。</p>
      <dl v-if="!compact">
        <dt>影响对象</dt>
        <dd>剧情变量 / MVU 变量、脚本变量，以及对话内角色设定和世界书。</dd>
        <dt>作用层级</dt>
        <dd>
          {{
            global
              ? '作为未单独选择保存方式的对话的默认值。修改保存在各对话的剧情进度中。'
              : '仅当前对话的剧情进度；继承进度时一并复制。'
          }}
        </dd>
      </dl>
      <p v-if="global">未单独选择保存方式的同模式对话，从下一轮起沿用此默认值。</p>
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
