<script setup lang="ts">
import { computed } from 'vue';
import MemoryRecords from '../records/MemoryRecords.vue';
const props = defineProps<{
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
}>();
const emit = defineEmits<{ change: [value: any]; cancel: []; locate: [source: string, excerpt: string] }>();
// H refs come with the branch messages from the backend; never count here.
const refs = computed(() => new Map<string, string>(props.messages.map((m) => [m.id, m.ref])));
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
      :refs="refs"
      @change="emit('change', $event)"
      @locate="(id, excerpt) => emit('locate', id, excerpt)"
    />
    <p v-else class="state-empty" role="status">正在读取记忆…</p>
  </div>
</template>
