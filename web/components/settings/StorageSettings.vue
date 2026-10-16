<script setup lang="ts">
import { useTimedError } from '../../composables/useTimedError';
import { onMounted, ref } from 'vue';
import { api } from '../../lib/api';
defineProps<{ settings: any }>();
const report = ref<any>(null),
  error = ref(''),
  busy = ref(false);
const size = (bytes: number) => `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
async function refresh() {
  try {
    report.value = await api('/storage');
  } catch (e: any) {
    error.value = e.message;
  }
}
async function clean() {
  busy.value = true;
  error.value = '';
  try {
    report.value = await api('/storage/cleanup', { vacuum: true });
  } catch (e: any) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
onMounted(refresh);
useTimedError(error);
</script>
<template>
  <p class="muted">只清理过期详细记录和可重建索引。剧情、分支、快照和可恢复的失败回合始终保留。</p>
  <div class="settings-field-grid">
    <label
      >详细记录保留天数<input type="number" min="1" max="365" v-model.number="settings.log_retention_days"
    /></label>
    <label
      >详细记录上限（MiB）<input type="number" min="1" max="16384" v-model.number="settings.log_max_mib"
    /></label>
    <label
      >检索缓存上限（MiB）<input
        type="number"
        min="1"
        max="16384"
        v-model.number="settings.retrieval_cache_max_mib"
    /></label>
  </div>
  <p class="muted">保存设置后生效。空闲时每小时检查；仍用于恢复的回合可能暂时超过限额。</p>
  <h4>当前占用</h4>
  <div v-if="report" class="storage-report">
    <div>
      <small>数据库</small><strong>{{ size(report.database_bytes) }}</strong>
    </div>
    <div>
      <small>详细记录</small><strong>{{ size(report.detail_bytes) }}</strong>
    </div>
    <div>
      <small>检索缓存</small><strong>{{ size(report.cache_bytes) }}</strong>
    </div>
    <div>
      <small>参谋 agent 详细记录</small><strong>{{ size(report.advisor_detail_bytes || 0) }}</strong>
    </div>
    <p>
      按已保存设置可回收
      {{
        size(
          report.reclaimable_bytes + report.reclaimable_cache_bytes + (report.advisor_reclaimable_bytes || 0),
        )
      }}，涉及 {{ report.turns_to_compact }} 个回合。受保护记录 {{ size(report.protected_bytes) }} · 模型权重
      {{ size(report.directories.models) }}（单独管理）
    </p>
  </div>
  <AppNotification :message="error" />
  <div class="actions">
    <button :disabled="busy" @click="refresh">刷新清理预览</button>
    <button :disabled="busy || !report" @click="clean">
      {{ busy ? '正在清理…' : '清理可回收数据并收缩数据库' }}
    </button>
  </div>
</template>
