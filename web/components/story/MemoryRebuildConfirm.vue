<script setup lang="ts">
import type { MemoryRebuildEstimate } from '../../lib/types';

defineProps<{ estimate: MemoryRebuildEstimate | null; disabled: boolean }>();
const emit = defineEmits<{ start: []; cancel: [] }>();
</script>

<template>
  <h2>重新生成记忆</h2>
  <p>
    预计至少 {{ estimate?.estimated_batches }} 次分批整理，最多 {{ estimate?.review_max_calls }} 次疑难复核。
  </p>
  <p class="muted">
    {{ estimate?.message }}
    重建会重新核对当前分支全部原文，保留手工修订和删除标记。实际 API 用量在后台工作记录中查看。
  </p>
  <div class="actions">
    <button :disabled="disabled" @click="emit('start')">开始重建</button>
    <button @click="emit('cancel')">取消</button>
  </div>
</template>
