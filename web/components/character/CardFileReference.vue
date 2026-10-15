<script setup lang="ts">
import { ref } from 'vue';

const emit = defineEmits<{ add: [path: string]; cancel: [] }>();
const path = ref('');
const error = ref('');

function add() {
  const value = path.value.trim().replace(/^"(.*)"$/, '$1');
  if (!/^(?:[a-z]:[\\/]|\/(?!\/))/i.test(value) || value.includes('\0')) {
    error.value = '请填写文件的完整路径，例如 C:\\资料\\设定.docx 或 /home/me/设定.md。';
    return;
  }
  emit('add', value);
}
</script>

<template>
  <form class="file-reference" @submit.prevent="add">
    <h2>引用本地文件</h2>
    <p>支持 TXT、Markdown、CSV、JSON、DOCX、XLSX 等文件，单个文件不超过 32 MiB。</p>
    <label for="card-file-path">文件完整路径</label>
    <input
      id="card-file-path"
      v-model="path"
      data-initial-focus
      required
      placeholder="例如 C:\资料\角色设定.docx"
      aria-describedby="card-file-help card-file-error"
      :aria-invalid="!!error"
      @input="error = ''"
    />
    <p id="card-file-help" class="file-help">
      在文件管理器中复制文件路径。添加后可补充要求，发送消息时由 agent
      读取。若从其他设备访问，请填写运行酒馆的电脑上的路径。
    </p>
    <p id="card-file-error" class="file-error" role="alert">{{ error }}</p>
    <div class="file-actions">
      <button type="button" @click="emit('cancel')">取消</button>
      <button class="primary" :disabled="!path.trim()">添加到消息</button>
    </div>
  </form>
</template>

<style scoped>
.file-reference h2 {
  margin: 0 0 16px;
  font: 600 20px/1.4 var(--font-ui);
}
.file-reference p {
  color: var(--text-secondary);
  font: 13px/1.8 var(--font-ui);
}
.file-reference label {
  display: block;
  margin: 20px 0 8px;
  font: 500 13px/1.5 var(--font-ui);
}
.file-reference input {
  width: 100%;
  font: 13px/1.6 var(--font-ui);
}
.file-reference .file-help {
  color: var(--text-muted);
  font-size: 12px;
}
.file-reference .file-error {
  min-height: 24px;
  color: var(--warning);
  overflow-wrap: anywhere;
}
.file-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 20px;
}
.file-actions button {
  font: 13px/1.5 var(--font-ui);
}
</style>
