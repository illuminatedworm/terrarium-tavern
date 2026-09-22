<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, type CSSProperties } from 'vue';

defineProps<{ tooltip: string }>();
const anchor = ref<HTMLElement>();
const bubble = ref<HTMLElement>();
const open = ref(false);
const position = ref<CSSProperties>({ left: '0px', top: '0px', visibility: 'hidden' });
const hovered = ref(false);
const focused = ref(false);

function place() {
  if (!anchor.value || !bubble.value) return;
  const rect = anchor.value.getBoundingClientRect();
  const { width, height } = bubble.value.getBoundingClientRect();
  const left = Math.max(8, Math.min(rect.left + rect.width / 2 - width / 2, innerWidth - width - 8));
  const above = rect.top - height - 8;
  const top = Math.max(8, Math.min(above >= 8 ? above : rect.bottom + 8, innerHeight - height - 8));
  position.value = { left: `${left}px`, top: `${top}px`, visibility: 'visible' };
}
async function update() {
  open.value = hovered.value || focused.value;
  if (!open.value) {
    window.removeEventListener('resize', place);
    window.removeEventListener('scroll', place, true);
    return;
  }
  position.value.visibility = 'hidden';
  await nextTick();
  place();
  window.addEventListener('resize', place);
  window.addEventListener('scroll', place, true);
}
onBeforeUnmount(() => {
  window.removeEventListener('resize', place);
  window.removeEventListener('scroll', place, true);
});
</script>

<template>
  <span
    ref="anchor"
    class="field-help"
    tabindex="0"
    role="note"
    :aria-label="tooltip"
    :data-tooltip="tooltip"
    @mouseenter="
      hovered = true;
      update();
    "
    @mouseleave="
      hovered = false;
      update();
    "
    @focus="
      focused = true;
      update();
    "
    @blur="
      focused = false;
      update();
    "
    @keydown.esc="
      hovered = false;
      focused = false;
      update();
    "
    >?</span
  >
  <Teleport to="body">
    <div v-if="open" ref="bubble" class="field-tooltip" role="tooltip" :style="position">
      {{ tooltip }}
    </div>
  </Teleport>
</template>
