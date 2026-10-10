<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue';
import { api } from '../../lib/api';
import { CHATGPT_USAGE_URL, chatgptState } from '../../lib/chatgptAccounts';
import AppModal from '../common/AppModal.vue';
const overlay = ref<HTMLElement>(),
  confirm = ref<HTMLButtonElement>();
const busy = ref(false),
  error = ref('');
let previous: Array<[HTMLElement, boolean]> = [];
let focus: HTMLElement | null = null;
function restore() {
  for (const [node, inert] of previous) node.inert = inert;
  previous = [];
  if (focus?.isConnected) focus.focus();
}
watch(
  () => chatgptState.welcome_required,
  async (show) => {
    if (!show) {
      restore();
      return;
    }
    focus = document.activeElement as HTMLElement;
    await nextTick();
    if (!overlay.value) return;
    previous = Array.from(document.body.children)
      .filter((node): node is HTMLElement => node instanceof HTMLElement && node !== overlay.value)
      .map((node) => [node, node.inert]);
    for (const [node] of previous) node.inert = true;
    confirm.value?.focus();
  },
  { immediate: true },
);
async function dismiss() {
  if (busy.value) return;
  busy.value = true;
  try {
    await api('/auth/chatgpt/welcome', {});
    chatgptState.welcome_required = false;
  } catch (value) {
    error.value = String(value);
  } finally {
    busy.value = false;
  }
}
onBeforeUnmount(restore);
</script>
<template>
  <Teleport to="body">
    <div
      v-if="chatgptState.welcome_required"
      ref="overlay"
      class="modal-backdrop chatgpt-welcome"
      @keydown.esc.prevent.stop="dismiss"
    >
      <AppModal label="正在使用 ChatGPT 订阅" size="small" @close="dismiss">
        <h2>正在使用你的 ChatGPT 订阅</h2>
        <p>
          选择此连接的 Agent 请求将计入你的 ChatGPT 订阅额度，并遵循你在 ChatGPT 中设置的额度及 credits
          使用权限。连接应用不会增加独立额度。
        </p>
        <p>各 Agent 仍使用各自选择的提供商和模型。</p>
        <p><a :href="CHATGPT_USAGE_URL" target="_blank" rel="noopener noreferrer">管理用量</a></p>
        <p v-if="error" role="alert">{{ error }}</p>
        <button ref="confirm" type="button" class="primary" :disabled="busy" @click="dismiss">知道了</button>
      </AppModal>
    </div>
  </Teleport>
</template>
