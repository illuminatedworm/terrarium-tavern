<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { api } from '../../lib/api';
import type { MessageTypeDefinition } from '../../lib/messageAdapter';
const props = defineProps<{ modelValue?: string; disabled?: boolean }>();
const emit = defineEmits<{ 'update:modelValue': [value: string] }>();
const types = ref<MessageTypeDefinition[]>([]),
  error = ref('');
const known = computed(() => types.value.some((type) => type.id === props.modelValue));
const choices = computed(() =>
  types.value.filter((type) => !type.id.startsWith('compat:') || type.id === props.modelValue),
);
onMounted(async () => {
  try {
    types.value = (await api('/message-adapter/types?lane=foreground')).filter(
      (type: MessageTypeDefinition) => type.id !== 'tool',
    );
  } catch (e) {
    error.value = String(e);
  }
});
</script>
<template>
  <label
    >运行时消息类型<select
      aria-label="运行时消息类型"
      :value="modelValue || ''"
      :disabled="disabled"
      @change="emit('update:modelValue', ($event.target as HTMLSelectElement).value)"
    >
      <option value="">按来源及旧角色自动识别</option>
      <option v-for="type in choices" :key="type.id" :value="type.id">{{ type.label }}</option>
      <option v-if="modelValue && !known" :value="modelValue">{{ modelValue }}</option></select
    ><small>{{ disabled ? '历史消息保留原类型。' : '发送给模型的角色和标签，在消息适配设置中调整。' }}</small
    ><small v-if="error" class="danger">{{ error }}</small></label
  >
</template>
