<script setup lang="ts">
import { audiences, emptyLoreFilter, type LoreFilter } from '../../lib/loreFilter';
defineProps<{ filter: LoreFilter; count: number; total: number }>();
</script>
<template>
  <div class="lore-filter">
    <label
      >搜索条目<input v-model="filter.query" type="search" placeholder="名称、关键词、ID 或正文"
    /></label>
    <label
      >启用状态<select v-model="filter.enabled" aria-label="启用状态">
        <option value="all">全部</option>
        <option value="enabled">启用</option>
        <option value="disabled">禁用</option>
      </select></label
    >
    <label
      >触发方式<select v-model="filter.trigger" aria-label="触发方式">
        <option value="all">全部</option>
        <option value="constant">常驻</option>
        <option value="conditional">条件触发</option>
      </select></label
    >
    <label
      >条目用途<select v-model="filter.audience" aria-label="条目用途">
        <option value="all">全部</option>
        <option v-for="(label, key) in audiences" :value="key">{{ label }}</option>
      </select></label
    >
    <span role="status">{{ count }} / {{ total }} 条</span>
    <button @click="Object.assign(filter, emptyLoreFilter())">清空筛选</button>
  </div>
</template>
<style scoped>
.lore-filter {
  display: flex;
  align-items: end;
  flex-wrap: wrap;
  gap: 10px;
  margin: 16px 0;
}
.lore-filter label {
  flex: 1 1 110px;
  min-width: 0;
}
.lore-filter label:first-child {
  flex-basis: 230px;
}
.lore-filter input,
.lore-filter select {
  width: 100%;
}
</style>
