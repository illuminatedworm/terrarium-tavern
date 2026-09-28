<script setup lang="ts">
import { nextTick, ref } from 'vue';
const props = defineProps<{
  modelValue: string;
  savedValue: string;
  label: string;
  saving?: boolean;
  error?: string;
  guard: (action: () => void) => void;
}>();
defineEmits<{ 'update:modelValue': [value: string]; save: [] }>();
const expanded = ref(false);
const trigger = ref<HTMLButtonElement>();
const large = ref<HTMLTextAreaElement>();
async function open() {
  expanded.value = true;
  await nextTick();
  large.value?.focus();
}
function close() {
  props.guard(() => {
    expanded.value = false;
    void nextTick(() => trigger.value?.focus());
  });
}
function trap(event: KeyboardEvent) {
  const nodes = Array.from(
    (event.currentTarget as HTMLElement).querySelectorAll<HTMLElement>('button:not([disabled]),textarea'),
  );
  if (event.shiftKey && document.activeElement === nodes[0]) {
    event.preventDefault();
    nodes.at(-1)?.focus();
  }
  if (!event.shiftKey && document.activeElement === nodes.at(-1)) {
    event.preventDefault();
    nodes[0]?.focus();
  }
}
</script>
<template>
  <div class="saved-text" :data-dirty="modelValue !== savedValue">
    <label :for="'text-' + label">{{ label }}</label>
    <div class="saved-text-field">
      <textarea
        :id="'text-' + label"
        :aria-label="label"
        :value="modelValue"
        spellcheck="false"
        @input="$emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)"
        @keydown.ctrl.enter.prevent="$emit('save')"
      />
      <div class="saved-text-actions">
        <button
          v-if="modelValue !== savedValue"
          class="text-save"
          type="button"
          :disabled="saving"
          :aria-label="'保存此文本：' + label"
          title="保存此文本 · Ctrl+Enter"
          @click="$emit('save')"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6" /></svg>
        </button>
        <button
          ref="trigger"
          class="expand-editor"
          type="button"
          :aria-label="'放大编辑：' + label"
          title="放大编辑"
          @click="open"
        >
          <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
            <path
              d="M12 3h5v5M17 3l-6 6M8 17H3v-5M3 17l6-6"
              stroke="currentColor"
              stroke-width="1.4"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
        </button>
      </div>
    </div>
    <span class="saved-text-status">{{ modelValue !== savedValue ? '有修改尚未保存' : '已保存' }}</span>
    <p v-if="error && modelValue !== savedValue" class="saved-text-error" role="alert">{{ error }}</p>
  </div>
  <Teleport to="body">
    <div v-if="expanded" class="expanded-editor-backdrop">
      <section
        class="expanded-editor settings-text-dialog"
        role="dialog"
        aria-modal="true"
        :aria-label="'编辑：' + label"
        @keydown.esc.prevent.stop="close"
        @keydown.tab="trap"
      >
        <header>
          <h2>{{ label }}</h2>
          <button aria-label="关闭放大编辑" @click="close">×</button>
        </header>
        <textarea
          ref="large"
          :aria-label="'编辑内容：' + label"
          :value="modelValue"
          spellcheck="false"
          @input="$emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)"
          @keydown.ctrl.enter.prevent="$emit('save')"
        />
        <footer>
          <span>{{ modelValue !== savedValue ? '有修改尚未保存' : '已保存' }}</span>
          <button
            v-if="modelValue !== savedValue"
            class="text-save"
            :disabled="saving"
            :aria-label="'保存此文本：' + label"
            @click="$emit('save')"
          >
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6" /></svg>
          </button>
        </footer>
        <p v-if="error && modelValue !== savedValue" class="saved-text-error" role="alert">{{ error }}</p>
      </section>
    </div>
  </Teleport>
</template>
<style scoped>
.saved-text {
  position: relative;
}
.saved-text-field {
  position: relative;
}
.saved-text-error {
  color: var(--danger);
  font-size: 13px;
  overflow-wrap: anywhere;
  margin: 0;
}
.saved-text-field > textarea {
  display: block;
  width: 100%;
  min-height: 280px;
  height: clamp(240px, 40vh, 460px);
  margin: 0;
  padding-bottom: 44px;
  resize: vertical;
  line-height: 1.65;
}
.saved-text-actions {
  display: flex;
  align-items: center;
  gap: 7px;
  position: absolute;
  right: 24px;
  bottom: 7px;
}
.saved-text-status {
  display: block;
  margin-top: 6px;
  color: var(--text-muted);
  font-size: 12px;
}
.saved-text-actions button {
  display: grid;
  place-items: center;
  min-width: 0;
  width: 26px;
  height: 26px;
  padding: 0;
  border-radius: 5px;
}
.saved-text-actions .expand-editor {
  border: 1px solid var(--border-subtle);
  background: var(--surface-panel);
  color: var(--text-muted);
}
.saved-text-actions .expand-editor:hover {
  background: var(--surface-hover);
  color: var(--accent-hover);
}
.saved-text-actions .expand-editor svg {
  width: 14px;
  height: 14px;
}
.text-save {
  display: grid;
  place-items: center;
  min-width: 40px;
  height: 38px;
  padding: 6px 10px;
  background: #39714a;
  border-color: #73c48e;
  color: #e7ffed;
}
.text-save:hover {
  background: #458559;
}
.text-save svg {
  width: 24px;
  height: 24px;
  fill: none;
  stroke: currentColor;
  stroke-width: 3;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.saved-text-actions .text-save svg {
  width: 18px;
  height: 18px;
}
.expanded-editor-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1500;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  background: #0009;
  backdrop-filter: blur(5px);
}
.settings-text-dialog {
  display: flex;
  flex-direction: column;
  width: min(960px, 100%);
  height: min(800px, 85dvh);
  min-height: 0;
  padding: 22px;
  gap: 16px;
  background: var(--surface-panel);
  border: 1px solid var(--border-control);
  border-radius: 12px;
  box-shadow: 0 24px 80px #0008;
}
.settings-text-dialog header,
.settings-text-dialog footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  height: auto;
  padding: 0;
  gap: 12px;
}
.settings-text-dialog h2 {
  margin: 0;
  font-size: 18px;
}
.settings-text-dialog textarea {
  flex: 1;
  min-height: 0;
  resize: none;
  line-height: 1.65;
}
@media (max-width: 760px) {
  .saved-text-actions button {
    width: 32px;
    height: 32px;
  }
}
</style>
