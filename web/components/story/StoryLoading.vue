<script setup lang="ts">
defineProps<{ creating?: boolean; name?: string; error?: string }>();
defineEmits<{ retry: []; back: [] }>();
</script>
<template>
  <section class="story-loading" aria-label="故事载入" :aria-busy="!error" role="status">
    <div class="story-loading-content">
      <div v-if="!error" class="story-loading-spinner" aria-hidden="true" />
      <h2>{{ error ? '暂时无法载入故事' : creating ? '正在进入新故事' : '正在载入对话' }}</h2>
      <p v-if="name" class="story-loading-name">{{ name }}</p>
      <p v-if="error" class="story-loading-error" role="alert">{{ error }}</p>
      <p v-else class="story-loading-hint">
        {{ creating ? '正在准备开场与角色资料…' : '正在读取剧情与对话状态…' }}
      </p>
      <div v-if="error" class="story-loading-actions">
        <button class="primary" @click="$emit('retry')">重试载入</button
        ><button @click="$emit('back')">返回</button>
      </div>
    </div>
  </section>
</template>
<style scoped>
.story-loading {
  flex: 1;
  display: grid;
  place-items: center;
  min-height: 0;
  overflow: auto;
  padding: 32px 24px;
  background: var(--surface-base);
}
.story-loading-content {
  max-width: 440px;
  text-align: center;
}
.story-loading-spinner {
  width: 34px;
  height: 34px;
  margin: 0 auto 24px;
  border: 3px solid var(--border-subtle);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: story-loading-spin 0.9s linear infinite;
}
.story-loading h2 {
  margin: 0 0 12px;
  font-size: 20px;
  font-weight: 600;
  color: var(--text-primary);
}
.story-loading-name {
  margin: 0 0 8px;
  font-size: 14px;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
.story-loading-hint {
  font-size: 13px;
  color: var(--text-muted);
}
.story-loading-error {
  color: var(--danger);
  font-size: 13px;
  overflow-wrap: anywhere;
}
.story-loading-actions {
  display: flex;
  gap: 10px;
  justify-content: center;
  margin-top: 22px;
}
@keyframes story-loading-spin {
  to {
    transform: rotate(360deg);
  }
}
@media (prefers-reduced-motion: reduce) {
  .story-loading-spinner {
    animation: none;
  }
}
</style>
