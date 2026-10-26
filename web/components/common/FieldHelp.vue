<script setup lang="ts">
import { useAnchoredTip } from '../../composables/useAnchoredTip';

defineProps<{ tooltip: string }>();
const { anchor, bubble, open, position, listeners } = useAnchoredTip();
</script>

<template>
  <span
    ref="anchor"
    class="field-help"
    tabindex="0"
    role="note"
    :aria-label="tooltip"
    :data-tooltip="tooltip"
    v-on="listeners"
    >?</span
  >
  <Teleport to="body">
    <div v-if="open" ref="bubble" class="field-tooltip" role="tooltip" :style="position">
      {{ tooltip }}
    </div>
  </Teleport>
</template>
