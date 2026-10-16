<script setup lang="ts">
import Icon from './Icon.vue';
defineProps<{ label: string; size?: 'small' | 'medium' | 'large' }>();
defineEmits<{ close: [] }>();

function trapFocus(event: KeyboardEvent) {
  const nodes = Array.from(
    (event.currentTarget as HTMLElement).querySelectorAll<HTMLElement>(
      'button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),summary,[tabindex="0"]',
    ),
  ).filter((node) => node.getClientRects().length);
  const first = nodes[0],
    last = nodes.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last?.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first?.focus();
  }
}
</script>

<template>
  <section
    class="modal"
    :class="[`modal-${size || 'medium'}`, { wide: size === 'large' }]"
    role="dialog"
    aria-modal="true"
    :aria-label="label"
    @keydown.tab="trapFocus"
  >
    <button class="close" aria-label="关闭" title="关闭" @click="$emit('close')">
      <Icon name="close" :size="16" />
    </button>
    <div class="modal-scroll"><slot /></div>
  </section>
</template>
