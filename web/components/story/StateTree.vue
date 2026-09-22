<script setup lang="ts">
import { computed } from 'vue';
import StoryProse from './StoryProse.vue';
const props = defineProps<{ value: any; depth?: number }>();
const structured = computed(() => props.value !== null && typeof props.value === 'object');
const entries = computed(() => (structured.value ? Object.entries(props.value) : []));
const labels: Record<string, string> = {
  summary: '剧情摘要',
  content: '内容',
  text: '正文',
  description: '描述',
  memory: '记忆',
  state: '状态',
  stat_data: '场景变量',
  user: '玩家',
  facts: '已知事实',
  events: '事件',
  characters: '角色',
  relationships: '关系',
};
function label(key: string) {
  return Array.isArray(props.value) ? `第 ${Number(key) + 1} 项` : labels[key] || key;
}
function display(value: any) {
  return value == null ? '—' : typeof value === 'boolean' ? (value ? '是' : '否') : String(value);
}
</script>
<template>
  <div class="state-tree">
    <p v-if="structured && !entries.length" class="state-empty">暂无内容</p>
    <template v-else-if="structured"
      ><template v-for="[key, entry] in entries" :key="key">
        <details v-if="entry !== null && typeof entry === 'object'" :open="(depth || 0) < 1">
          <summary>{{ label(key) }}</summary>
          <StateTree :value="entry" :depth="(depth || 0) + 1" />
        </details>
        <div v-else class="state-leaf">
          <span>{{ label(key) }}</span
          ><StoryProse v-if="typeof entry === 'string' && entry.includes('\n')" :text="entry" /><strong
            v-else
            :class="{ numeric: typeof entry === 'number' }"
            >{{ display(entry) }}</strong
          >
        </div>
      </template></template
    >
    <StoryProse v-else-if="typeof value === 'string' && value" :text="value" />
    <p v-else class="state-empty">{{ display(value) }}</p>
  </div>
</template>
