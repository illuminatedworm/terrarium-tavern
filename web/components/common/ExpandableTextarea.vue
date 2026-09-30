<script setup lang="ts">
import { useTimedError } from '../../composables/useTimedError';
import { nextTick, ref } from 'vue';
import PromptPrefix from './PromptPrefix.vue';
const props = defineProps<{
  modelValue?: string | null;
  label: string;
  placeholder?: string;
  prefix?: string;
  code?: boolean;
  compact?: boolean;
  disabled?: boolean;
  rows?: number;
  submitOnEnter?: boolean;
  expandOnFocus?: boolean;
  validate?: (value: string) => string;
}>();
const emit = defineEmits<{ 'update:modelValue': [value: string]; change: [value: string]; submit: [] }>();
const expanded = ref(false),
  draft = ref(''),
  error = ref('');
const inline = ref<HTMLTextAreaElement>(),
  large = ref<HTMLTextAreaElement>(),
  trigger = ref<HTMLButtonElement>();
async function openEditor() {
  draft.value = props.modelValue || '';
  error.value = '';
  const start = inline.value?.selectionStart || 0,
    end = inline.value?.selectionEnd || 0;
  expanded.value = true;
  await nextTick();
  large.value?.focus();
  large.value?.setSelectionRange(start, end);
}
async function closeEditor() {
  expanded.value = false;
  await nextTick();
  if (props.expandOnFocus) inline.value?.focus();
  else trigger.value?.focus();
}
function apply() {
  error.value = props.validate?.(draft.value) || '';
  if (error.value) return;
  emit('update:modelValue', draft.value);
  emit('change', draft.value);
  closeEditor();
}
function trapFocus(event: KeyboardEvent) {
  const controls = Array.from(
    (event.currentTarget as HTMLElement).querySelectorAll<HTMLElement>('button, textarea'),
  );
  const first = controls[0],
    last = controls.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last?.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first?.focus();
  }
}
useTimedError(error);
</script>

<template>
  <div
    class="expandable-textarea"
    :class="{ 'is-code': code, 'is-compact': compact, 'expand-on-focus': expandOnFocus }"
  >
    <PromptPrefix v-if="prefix !== undefined" :text="prefix" :label="`${label}的系统预制头`" />
    <textarea
      ref="inline"
      :value="modelValue || ''"
      :aria-label="label"
      :placeholder="placeholder"
      :disabled="disabled"
      :rows="rows"
      :spellcheck="code ? false : undefined"
      @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)"
      @change="emit('change', ($event.target as HTMLTextAreaElement).value)"
      @keydown.enter.exact="submitOnEnter && ($event.preventDefault(), emit('submit'))"
    />
    <button
      ref="trigger"
      type="button"
      class="expand-editor"
      :aria-label="`放大编辑：${label}`"
      title="放大编辑"
      :disabled="disabled"
      @click.prevent="openEditor"
    >
      <svg width="14" height="14" viewBox="0 0 20 20" fill="none" aria-hidden="true">
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
  <Teleport to="body">
    <div v-if="expanded" class="expanded-editor-backdrop">
      <section
        class="expanded-editor"
        role="dialog"
        aria-modal="true"
        :aria-label="`编辑：${label}`"
        @keydown.esc.prevent.stop="closeEditor"
        @keydown.tab="trapFocus"
        @keydown.ctrl.enter.prevent="apply"
      >
        <header class="expanded-editor-heading">
          <h2>{{ label }}</h2>
          <button type="button" aria-label="关闭放大编辑" @click="closeEditor">×</button>
        </header>
        <PromptPrefix v-if="prefix !== undefined" :text="prefix" :label="`${label}的系统预制头`" />
        <textarea
          ref="large"
          v-model="draft"
          :class="{ 'is-code': code }"
          :aria-label="`编辑内容：${label}`"
          :spellcheck="code ? false : undefined"
          @input="error = ''"
        />
        <AppNotification :message="error" />
        <footer>
          <span>Ctrl + Enter 应用 · Esc 取消</span><button type="button" @click="closeEditor">取消</button
          ><button type="button" class="primary" @click="apply">应用修改</button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.expandable-textarea {
  position: relative;
  width: 100%;
  margin-top: 8px;
}
.expandable-textarea > textarea {
  display: block;
  width: 100%;
  height: 144px;
  min-height: 144px;
  max-height: 144px;
  margin: 0;
  padding: 11px 12px 36px;
  resize: none;
  font-size: 14px;
  line-height: 1.75;
}
.expandable-textarea.is-code > textarea {
  font:
    12px/1.7 Consolas,
    monospace;
}
.expandable-textarea.is-compact {
  margin-top: 0;
}
.expandable-textarea.is-compact > textarea {
  height: auto;
  min-height: 70px;
  max-height: 180px;
  padding-right: 42px;
  resize: vertical;
}
.expand-editor {
  position: absolute;
  bottom: 7px;
  right: 7px;
  display: grid;
  place-items: center;
  width: 26px;
  height: 26px;
  padding: 0;
  border: 1px solid var(--border-subtle);
  border-radius: 5px;
  background: var(--surface-panel);
  color: var(--text-muted);
}
.expand-editor:hover {
  background: var(--surface-hover);
  color: var(--accent-hover);
}
.expand-on-focus > .expand-editor {
  visibility: hidden;
  pointer-events: none;
}
.expand-on-focus:focus-within > .expand-editor {
  visibility: visible;
  pointer-events: auto;
}
.expanded-editor-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1500;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #0009;
  backdrop-filter: blur(5px);
  padding: 16px;
}
.expanded-editor {
  display: flex;
  flex-direction: column;
  width: clamp(560px, 50vw, 960px);
  max-width: 100%;
  height: 80dvh;
  overflow: hidden;
  border: 1px solid var(--border-control);
  border-radius: 12px;
  background: var(--surface-panel);
  box-shadow: 0 24px 80px #0008;
}
.expanded-editor-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-shrink: 0;
  height: auto;
  padding: 16px 20px;
  gap: 16px;
  border-bottom: 1px solid var(--border-subtle);
}
.expanded-editor-heading h2 {
  font-size: 16px;
  margin: 0;
  letter-spacing: normal;
}
.expanded-editor-heading button {
  width: 26px;
  height: 26px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--text-muted);
  font-size: 23px;
}
.expanded-editor > textarea {
  flex: 1;
  min-height: 0;
  width: auto;
  margin: 16px 20px;
  resize: none;
  padding: 14px;
  color: var(--text-story);
  font-size: 15px;
  line-height: 1.8;
}
.expanded-editor > .prompt-prefix {
  flex-shrink: 0;
  margin: 16px 20px 0;
}
.expanded-editor > textarea.is-code {
  font:
    13px/1.75 Consolas,
    monospace;
}
.expanded-editor-error {
  flex-shrink: 0;
  margin: 0 20px 12px;
  color: var(--danger);
  font-size: 12px;
}
.expanded-editor footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  flex-shrink: 0;
  padding: 12px 20px;
  border-top: 1px solid var(--border-subtle);
}
.expanded-editor footer span {
  margin-right: auto;
  color: var(--text-muted);
  font-size: 10px;
}
.expanded-editor footer button {
  padding: 7px 12px;
  font-size: 12px;
}
@media (max-width: 620px) {
  .expanded-editor {
    width: 100%;
    height: 84dvh;
  }
  .expanded-editor footer span {
    display: none;
  }
  .expanded-editor > textarea {
    margin: 12px;
  }
}
</style>
