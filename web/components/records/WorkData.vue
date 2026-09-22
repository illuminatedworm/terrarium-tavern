<script setup lang="ts">
import { computed, ref } from 'vue';
const props = defineProps<{ value: any; label: string; root?: boolean }>();
const opened = ref(false);
const branch = computed(() => props.value !== null && typeof props.value === 'object');
const entries = computed(() => (branch.value ? Object.entries(props.value) : []));
const multiline = computed(
  () => typeof props.value === 'string' && (props.value.length > 160 || props.value.includes('\n')),
);
const labels: Record<string, string> = {
  messages: '输入消息',
  tools: '可用工具',
  metadata: '活动数据',
  operations: '修改操作',
  before: '修改前',
  after: '修改后',
  content: '内容',
  message: '说明',
  role: '角色',
  usage: '用量',
  estimated_tokens: '估算 tokens',
  estimated_input_tokens: '估算输入 tokens',
  prompt_tokens: '实际输入 tokens',
  completion_tokens: '实际输出 tokens',
  cached_tokens: '缓存命中 tokens',
  token_counting: 'Token 计数口径',
  reason: '原因',
  purpose: '用途',
  model: '模型',
  state: '状态',
  memory: '记忆',
  memory_review: '疑难复核',
  history_continuation: '历史补查后的模型续答',
  duration_s: '耗时（秒）',
  evidence: '辅助资料及版本',
  estimated_batches: '预计批次数',
  source_ids: '本批原文来源',
  primary_calls: '本次基础整理请求数',
  questions: '待核验问题',
  scopes: '变量作用域',
  source: '来源',
  kind: '类型',
};
const count = computed(() =>
  branch.value
    ? `${entries.value.length} ${Array.isArray(props.value) ? '项' : '个字段'}`
    : `${Array.from(String(props.value ?? '')).length.toLocaleString()} 字符`,
);
function toggle(event: Event) {
  opened.value = (event.currentTarget as HTMLDetailsElement).open;
}
function childLabel(key: string) {
  return Array.isArray(props.value) ? `第 ${Number(key) + 1} 项` : labels[key] || key;
}
</script>

<template>
  <div v-if="root && branch" class="work-data-root">
    <WorkData v-for="[key, item] in entries" :key="key" :label="childLabel(key)" :value="item" />
  </div>
  <details v-else-if="branch || multiline" class="work-data-branch" @toggle="toggle">
    <summary>
      <strong>{{ label }}</strong
      ><small>{{ count }}</small>
    </summary>
    <div v-if="opened" class="work-data-body">
      <template v-if="branch"
        ><WorkData v-for="[key, item] in entries" :key="key" :label="childLabel(key)" :value="item"
      /></template>
      <pre v-else>{{ value }}</pre>
    </div>
  </details>
  <div v-else class="work-data-leaf">
    <span>{{ label }}</span
    ><code>{{ value === null ? (label.includes('tokens') ? '不可用' : 'null') : String(value) }}</code>
  </div>
</template>
