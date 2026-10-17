<script setup lang="ts">
import MemoryRecords from '../records/MemoryRecords.vue';
defineProps<{
  kind: 'memory';
  snapshot: any;
  messages: any[];
  persona?: string;
  character?: string;
  disabled?: boolean;
  saving?: boolean;
  memoryView?: any;
  progress?: string;
  layout?: 'list' | 'cards';
  rounds?: Map<string, number>;
}>();
const emit = defineEmits<{ change: [value: any]; cancel: []; locate: [source: string, excerpt: string] }>();
</script>
<template>
  <div class="scene-notes" :data-kind="kind">
    <p v-if="progress" role="status">{{ progress }} <button @click="emit('cancel')">取消重建</button></p>
    <MemoryRecords
      v-if="memoryView"
      :memory="memoryView"
      :disabled="disabled"
      :saving="saving"
      :layout="layout"
      :rounds="rounds"
      @change="emit('change', $event)"
      @locate="(id, excerpt) => emit('locate', id, excerpt)"
    />
    <p v-else class="state-empty" role="status">正在读取记忆…</p>
  </div>
</template>
