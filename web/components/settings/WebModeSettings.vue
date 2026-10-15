<script setup lang="ts">
import { computed } from 'vue';
import type { WebMode, WebSettings } from '../../lib/settingsDocument';
import { webServiceLabel } from '../../lib/webSearch';
const props = defineProps<{ value: { mode: WebMode }; services: WebSettings }>();
defineEmits<{ services: [] }>();
const active = computed(() =>
  props.services.order
    .filter((name) => props.services[name].enabled && (name !== 'searxng' || props.services.searxng.endpoint))
    .map(webServiceLabel)
    .join(' → '),
);
</script>
<template>
  <fieldset>
    <legend>联网查阅</legend>
    <label
      >联网方式<select v-model="value.mode" aria-label="联网方式">
        <option value="auto">自动 · 官方 OAuth 渠道优先云端搜索</option>
        <option value="local">本地实现 · 使用共享联网服务</option>
        <option value="off">关闭</option>
      </select></label
    >
    <p class="muted">普通渠道使用本地接入；本地实现仍需联网。搜索失败会依次降级并显示原因。</p>
    <p v-if="value.mode !== 'off'" class="muted">
      共享本地搜索顺序：{{ active || '未启用服务' }}。网页提取依次使用已启用的 Exa /
      Tavily，最后使用本地网页读取。
    </p>
    <button type="button" @click="$emit('services')">配置共享联网服务与密钥</button>
  </fieldset>
</template>
<style scoped>
fieldset {
  display: grid;
  gap: 12px;
  min-width: 0;
}
label {
  display: grid;
  gap: 6px;
}
.muted {
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.7;
  margin: 0;
  overflow-wrap: anywhere;
}
button {
  justify-self: start;
}
</style>
