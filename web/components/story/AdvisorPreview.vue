<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { rawApi } from '../../lib/api';
import AdvisorIcon from './AdvisorIcon.vue';
const props = defineProps<{ sessionId: string }>();
const emit = defineEmits<{ close: [] }>();
const dialog = ref<HTMLDialogElement>(),
  preview = ref<any>(null),
  error = ref('');
onMounted(async () => {
  dialog.value?.showModal();
  try {
    preview.value = await rawApi(`/sessions/${props.sessionId}/advisor/preview`);
  } catch (e) {
    error.value = String(e);
  }
});
</script>
<template>
  <Teleport to="body">
    <dialog
      ref="dialog"
      class="advisor-preview"
      aria-label="参谋 agent 上下文"
      @cancel.prevent="emit('close')"
      @click="$event.target === dialog && emit('close')"
    >
      <header>
        <h2>参谋 agent 上下文</h2>
        <button aria-label="关闭参谋 agent 上下文" @click="emit('close')">
          <AdvisorIcon name="close" />
        </button>
      </header>
      <div class="advisor-preview-body">
        <p v-if="error" role="alert">{{ error }}</p>
        <p v-else-if="!preview" role="status">正在编译预览…</p>
        <template v-else>
          <p class="muted">
            只读预览 · {{ preview.estimated_tokens.toLocaleString() }} 估算 tokens /
            {{ preview.context_limit.toLocaleString() }} · {{ preview.recent_refs.length }} 条剧情原文
          </p>
          <p class="muted">下次咨询的背景预览，不含尚未发送的问题。运行中的咨询使用开始时冻结的剧情。</p>
          <p v-if="preview.token_counting.source === 'reference_fallback'" class="muted">
            未知模型使用参考分词器 {{ preview.token_counting.tokenizer }}；估算不等同于服务商实际用量。
          </p>
          <p v-if="preview.opaque" class="muted">
            包含参谋 agent 独立的云端压缩状态；本地估算不包含密文占用。
          </p>
          <p v-if="preview.summary" class="muted">已包含旧咨询摘要。摘要不属于故事记忆。</p>
          <details v-for="(message, i) in preview.messages" :key="i" :open="i === 0">
            <summary>{{ i + 1 }} · {{ message.role }}</summary>
            <pre>{{
              message._native_response ? '云端压缩状态（加密内容由服务端解释）' : message.content
            }}</pre>
          </details>
          <details>
            <summary>可用工具 · {{ preview.tools.length }}</summary>
            <pre>{{ JSON.stringify(preview.tools, null, 2) }}</pre>
          </details>
        </template>
      </div>
    </dialog>
  </Teleport>
</template>
<style scoped>
.advisor-preview {
  width: min(920px, calc(100vw - 32px));
  height: min(780px, calc(100dvh - 48px));
  padding: 0;
  border: 1px solid var(--border-control);
  border-radius: 16px;
  background: var(--surface-panel);
  color: var(--text-primary);
  overflow: hidden;
}
.advisor-preview::backdrop {
  background: #0009;
}
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 20px 24px;
  border-bottom: 1px solid var(--border-subtle);
  height: 70px;
  box-sizing: border-box;
}
h2 {
  margin: 0;
  font-size: 18px;
}
button {
  width: 32px;
  height: 32px;
  padding: 6px;
}
svg {
  width: 20px;
  height: 20px;
}
.advisor-preview-body {
  height: calc(100% - 70px);
  overflow: auto;
  padding: 20px 24px;
  box-sizing: border-box;
}
details {
  border-bottom: 1px solid var(--border-subtle);
  padding: 12px 0;
}
summary {
  font-size: 13px;
  cursor: pointer;
}
pre {
  font: 13px/1.7 var(--font-ui);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
