<script setup lang="ts">
import { channelLabels, type Channel } from '../../lib/channels';
defineProps<{ modelValue: Channel; disabled?: boolean; label?: string }>();
const emit = defineEmits<{ 'update:modelValue': [channel: Channel] }>();
const channels: Channel[] = ['standard', 'chatgpt'];
function move(event: KeyboardEvent, channel: Channel) {
  emit('update:modelValue', channel);
  const parent = (event.currentTarget as HTMLElement).parentElement;
  parent?.querySelector<HTMLButtonElement>(`[data-channel="${channel}"]`)?.focus();
}
</script>
<template>
  <div class="channel-switch" role="tablist" :aria-label="label || '连接渠道'">
    <button
      v-for="channel in channels"
      :key="channel"
      type="button"
      role="tab"
      :data-channel="channel"
      :aria-selected="modelValue === channel"
      :tabindex="modelValue === channel ? 0 : -1"
      :disabled="disabled"
      :class="{ selected: modelValue === channel }"
      @click="emit('update:modelValue', channel)"
      @keydown.right.prevent="move($event, channel === 'standard' ? 'chatgpt' : 'standard')"
      @keydown.left.prevent="move($event, channel === 'standard' ? 'chatgpt' : 'standard')"
    >
      {{ channelLabels[channel] }}
    </button>
  </div>
</template>
<style scoped>
.channel-switch {
  display: flex;
  gap: 8px;
  border-bottom: 1px solid var(--surface-hover);
  margin: 0 0 20px;
}
.channel-switch button {
  background: transparent;
  border: 0;
  border-bottom: 2px solid transparent;
  border-radius: 0;
  padding: 12px 18px;
  color: var(--text-secondary);
  font-size: 14px;
}
.channel-switch button.selected {
  color: var(--accent-hover);
  border-bottom-color: var(--accent);
  background: transparent;
}
@media (max-width: 600px) {
  .channel-switch button {
    flex: 1;
    padding: 12px 8px;
  }
}
</style>
