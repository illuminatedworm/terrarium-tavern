<script setup lang="ts">
import { ref } from 'vue';
import type { MessageAdapterConfig } from '../../lib/messageAdapter';
import MessageAdapterEditor from './MessageAdapterEditor.vue';

defineProps<{
  modelValue?: MessageAdapterConfig;
  apiMode: string;
  lane: string;
  typeIds?: string[];
}>();
defineEmits<{ 'update:modelValue': [value: MessageAdapterConfig] }>();
const editor = ref<InstanceType<typeof MessageAdapterEditor>>();
async function resetRoles(apiMode: string) {
  await editor.value?.resetRoles(apiMode);
}
defineExpose({ resetRoles });
</script>

<template>
  <details class="settings-advanced global-message-adapter">
    <summary>消息适配：发送角色与标签包裹</summary>
    <p class="muted">这里只影响新建的游玩或制卡对话。已有对话在自己的消息适配设置中修改。</p>
    <MessageAdapterEditor
      ref="editor"
      :model-value="modelValue"
      :api-mode="apiMode"
      :lane="lane"
      :type-ids="typeIds"
      @update:model-value="$emit('update:modelValue', $event)"
    />
  </details>
</template>
