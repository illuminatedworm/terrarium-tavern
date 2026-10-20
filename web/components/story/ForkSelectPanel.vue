<script setup lang="ts">
import type { BranchMessage } from '../../lib/types';

/** Picks the reply whose progress a new conversation inherits. */
defineProps<{ points: BranchMessage[]; head: string; name: string }>();
const emit = defineEmits<{ pick: [point: BranchMessage]; cancel: [] }>();
</script>

<template>
  <h2>新建对话</h2>
  <p class="muted">选择要继承的对话进度。新对话将从所选回复结束的位置继续，并保留该位置的记忆与状态快照。</p>
  <div class="branch-list">
    <button
      v-for="(point, index) in points"
      :key="point.id"
      class="branch"
      :class="{ current: point.id === head }"
      :title="point.raw"
      @click="emit('pick', point)"
    >
      <small class="branch-index">{{ index + 1 }}</small
      ><span class="branch-role">{{ name }}</span
      ><span class="branch-excerpt">{{ point.raw.replace(/\s+/g, ' ').trim() || '（空回复）' }}</span
      ><small v-if="point.id === head" class="branch-current">当前</small>
    </button>
  </div>
  <p v-if="!points.length" class="muted">暂无可继承的 AI 回复。</p>
  <div class="modal-footer"><button @click="emit('cancel')">取消</button></div>
</template>
