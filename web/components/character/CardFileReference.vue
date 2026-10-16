<script setup lang="ts">
import { ref } from 'vue';
import CardWorkspaceIcon from './CardWorkspaceIcon.vue';
defineProps<{ disabled: boolean; uploading: boolean }>();
const emit = defineEmits<{ choose: [files: File[]] }>();
const picker = ref<HTMLInputElement>();
function choose(event: Event) {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files || []);
  input.value = '';
  if (files.length) emit('choose', files);
}
</script>
<template>
  <button
    class="cw-reference-add cw-quiet"
    type="button"
    :disabled="disabled"
    title="选择文本、DOCX、XLSX 文件或 PNG、JPEG、静态 WebP 图片；每个附件最多 32 MiB"
    @click="picker?.click()"
  >
    <CardWorkspaceIcon name="file" />{{ uploading ? '正在添加文件…' : '引用本地文件' }}
  </button>
  <input
    ref="picker"
    type="file"
    hidden
    multiple
    aria-label="选择本地参考文件"
    accept=".txt,.md,.markdown,.csv,.tsv,.json,.json5,.yaml,.yml,.xml,.html,.log,.ini,.toml,.rst,.docx,.xlsx,.png,.jpg,.jpeg,.webp"
    :disabled="disabled"
    @change="choose"
  />
</template>

<style scoped>
button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  padding: 7px 9px;
  font: 12px/1.5 var(--font-ui);
  color: var(--text-secondary);
  background: transparent;
  border-color: transparent;
}
button:hover:not(:disabled) {
  color: var(--text-primary);
  background: var(--surface-hover);
}
button svg {
  width: 16px;
  height: 16px;
  color: var(--accent-hover);
}
</style>
