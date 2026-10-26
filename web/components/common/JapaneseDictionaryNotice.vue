<script setup lang="ts">
import { inject } from 'vue';
import { japaneseDictionaryKey } from '../../composables/useJapaneseDictionary';
const { status, busy, label, progress } = inject(japaneseDictionaryKey)!;
defineEmits<{ settings: [] }>();
</script>
<template>
  <div
    v-if="
      status?.platform === 'desktop' && (busy || status.state === 'error' || status.state === 'cancelled')
    "
    class="channel-notice"
    role="status"
  >
    <span
      >日文词典：{{ label
      }}<template v-if="progress !== undefined"> {{ Math.floor(progress * 100) }}%</template
      ><template v-if="status.error"> · {{ status.error }}</template></span
    >
    <button class="quiet" @click="$emit('settings')">查看与重试</button>
  </div>
</template>
