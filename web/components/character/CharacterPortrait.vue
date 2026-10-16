<script setup lang="ts">
import { computed, ref, watch } from 'vue';

const props = withDefaults(
  defineProps<{
    src?: string;
    name?: string;
    size?: 'small' | 'message' | 'card' | 'detail';
    fit?: 'cover' | 'contain';
  }>(),
  { src: '', name: '', size: 'small', fit: 'cover' },
);
const failed = ref(false);
const initial = computed(() => Array.from(props.name.trim())[0] || '◇');
watch(
  () => props.src,
  () => {
    failed.value = false;
  },
);
</script>

<template>
  <span class="character-portrait" :class="`portrait-${size}`" aria-hidden="true">
    <img
      v-if="src && !failed"
      :key="src"
      :src="src"
      alt=""
      loading="lazy"
      decoding="async"
      :style="{ objectFit: fit }"
      @error="failed = true"
    />
    <span v-else>{{ initial }}</span>
  </span>
</template>

<style scoped>
.character-portrait {
  display: inline-grid;
  place-items: center;
  position: relative;
  flex-shrink: 0;
  width: 36px;
  height: 36px;
  overflow: hidden;
  border-radius: 9px;
  background: linear-gradient(145deg, rgb(var(--accent-rgb) / 0.3), rgb(var(--accent-rgb) / 0.1));
  box-shadow: inset 0 0 0 1px rgb(255 255 255 / 0.08);
  color: var(--accent-hover);
  font: 500 16px var(--font-ui);
}
.character-portrait img {
  display: block;
  width: 100%;
  height: 100%;
  min-width: 0;
  min-height: 0;
}
.portrait-message {
  width: 35px;
  height: 35px;
  border-radius: 11px;
  font-size: 15px;
}
.portrait-card {
  width: 64px;
  height: 80px;
  font-size: 24px;
}
.portrait-detail {
  width: 104px;
  height: 136px;
  border-radius: 16px;
  box-shadow:
    inset 0 0 0 1px rgb(255 255 255 / 0.1),
    0 18px 40px -18px var(--accent-glow, transparent);
  font-size: 32px;
}
@media (max-width: 620px) {
  .portrait-message {
    width: 29px;
    height: 29px;
  }
  .portrait-card {
    width: 56px;
    height: 70px;
  }
  .portrait-detail {
    width: 80px;
    height: 104px;
  }
}
</style>
