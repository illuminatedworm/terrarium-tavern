<script setup lang="ts">
defineProps<{ name?: string; busy: boolean }>();
const emit = defineEmits<{ cancel: []; confirm: [] }>();
</script>

<template>
  <h2>删除角色卡</h2>
  <p>彻底删除“{{ name }}”？角色卡及未被使用的资源会删除，已有对话保留继续游玩所需的设定和资源。</p>
  <div class="modal-footer">
    <button :disabled="busy" @click="emit('cancel')">取消</button
    ><button class="danger" :disabled="busy" @click="emit('confirm')">
      {{ busy ? '正在删除…' : '确认删除角色卡' }}
    </button>
  </div>
</template>
