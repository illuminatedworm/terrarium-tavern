<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue';
import AppModal from '../common/AppModal.vue';
const props = defineProps<{
  title: string;
  changes: number;
  hasInput: boolean;
  sourceExists: boolean;
  canSave: boolean;
  busy: boolean;
  error: string;
  background?: HTMLElement | null;
}>();
const emit = defineEmits<{ close: []; remove: [saveMode?: 'overwrite' | 'copy'] }>();
const cancel = ref<HTMLButtonElement>();
const previousFocus = document.activeElement as HTMLElement | null;
let wasInert = false;
onMounted(async () => {
  if (props.background) {
    wasInert = props.background.inert;
    props.background.inert = true;
  }
  await nextTick();
  cancel.value?.focus();
});
onBeforeUnmount(() => {
  if (props.background) props.background.inert = wasInert;
  if (previousFocus?.isConnected) previousFocus.focus();
});
function close() {
  if (!props.busy) emit('close');
}
</script>
<template>
  <Teleport to="body">
    <div class="modal-backdrop" @click.self="close" @keydown.esc.stop.prevent="close">
      <AppModal label="删除编辑对话" size="small" @close="close">
        <h2>删除编辑对话？</h2>
        <p class="delete-editor-title">{{ title }}</p>
        <p v-if="changes" class="delete-editor-warning">
          这份草稿有 {{ changes }} 项未保存修改。不保存就删除，这些修改将随草稿一起丢失。
        </p>
        <p v-else>这份草稿没有未保存的角色卡修改。</p>
        <p>
          将删除当前编辑对话的聊天、动作记录、草稿和撤销历史。已保存的角色卡、其他编辑对话及游玩对话不受影响。
        </p>
        <p v-if="hasInput">当前未发送的输入也会删除。</p>
        <p v-if="!sourceExists" class="delete-editor-warning">原卡已删除。如需保留这份草稿，请先另存副本。</p>
        <p v-if="error" class="delete-editor-warning" role="alert">{{ error }}</p>
        <div v-if="changes || !sourceExists" class="delete-editor-options">
          <button :disabled="busy || !canSave || !sourceExists" @click="emit('remove', 'overwrite')">
            覆盖保存后删除
          </button>
          <button :disabled="busy || !canSave" @click="emit('remove', 'copy')">另存副本后删除</button>
        </div>
        <div class="delete-editor-actions">
          <button ref="cancel" :disabled="busy" @click="close">取消</button>
          <button class="danger" :disabled="busy" @click="emit('remove')">
            {{ busy ? '正在处理…' : changes ? '不保存，删除对话' : '删除对话' }}
          </button>
        </div>
      </AppModal>
    </div>
  </Teleport>
</template>
<style scoped>
h2 {
  font-size: 20px;
  margin: 0 0 12px;
}
.delete-editor-title {
  color: var(--text-muted);
  overflow-wrap: anywhere;
}
.delete-editor-warning {
  color: var(--danger);
  overflow-wrap: anywhere;
}
.delete-editor-options,
.delete-editor-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 9px;
  margin-top: 18px;
}
.delete-editor-options button {
  flex: 1;
  font-size: 12px;
}
.delete-editor-actions {
  justify-content: flex-end;
}
</style>
