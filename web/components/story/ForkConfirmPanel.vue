<script setup lang="ts">
import type { BranchMessage } from '../../lib/types';

defineProps<{
  name: string;
  message: Pick<BranchMessage, 'raw'> | null;
  fromPicker: boolean;
  creating: boolean;
}>();
const emit = defineEmits<{ back: []; cancel: []; confirm: [] }>();
</script>

<template>
  <h2>继承进度，新建对话</h2>
  <p class="muted">
    继承这条回复及此前的完整对话，同时延续当时的场景变量、记忆、检索线索、世界书和脚本变量，以及上下文配方。新对话从这里独立继续，原对话保留。
  </p>
  <div class="fork-preview">
    <strong>{{ name }}</strong>
    <p>{{ message?.raw }}</p>
  </div>
  <div class="modal-footer">
    <button v-if="fromPicker" :disabled="creating" @click="emit('back')">返回选择</button
    ><button :disabled="creating" @click="emit('cancel')">取消</button
    ><button class="primary" :disabled="creating" @click="emit('confirm')">
      {{ creating ? '正在创建…' : '确认继承并新建对话' }}
    </button>
  </div>
</template>
