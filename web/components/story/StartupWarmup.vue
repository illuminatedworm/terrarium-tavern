<script setup lang="ts">
import { onMounted } from 'vue';
import {
  localModelLabels as labels,
  type StartupModels,
  type StartupWarmupState,
} from '../../composables/useStartupWarmup';

const props = defineProps<{ status: StartupModels; warmup: StartupWarmupState }>();
defineEmits<{ close: []; settings: [] }>();
const models = props.status.enabled;
const { jobs, pending, submitted, error, running } = props.warmup;
const available = models.filter((purpose) => props.status.models[purpose].downloaded);
const missing = models.filter((purpose) => !props.status.models[purpose].downloaded);
onMounted(() => {
  localStorage.setItem('tavern-warmup-startup', props.status.startup_id);
});
</script>

<template>
  <h2>{{ available.length ? '预热本地检索模型？' : '本地检索模型尚未下载' }}</h2>
  <p v-if="available.length">
    你已启用 {{ models.map((purpose) => labels[purpose]).join('、') }}。服务刚启动，是否现在加载
    {{ available.map((purpose) => labels[purpose]).join('、') }}？预热会占用内存或显存。
  </p>
  <p v-else>你已启用 {{ models.map((purpose) => labels[purpose]).join('、') }}，但模型文件尚未下载。</p>
  <p v-if="missing.length" class="muted">
    {{
      available.length ? `${missing.map((purpose) => labels[purpose]).join('、')} 尚未下载，` : ''
    }}请到设置中的“混合召回与本地模型”页面下载模型。
  </p>
  <p class="muted">
    {{
      available.length ? '暂不预热' : '下载前'
    }}也可继续使用；不可用的检索或重排通道会降级。已保存的世界书向量缓存仍可复用。
  </p>
  <AppNotification v-for="job in jobs" :key="`error-${job.id}`" :message="job.error || ''" />
  <p
    v-for="job in jobs.filter((j: any) => j.status !== 'error')"
    :key="job.id"
    role="status"
    :class="{ error: job.status === 'error' }"
  >
    {{ labels[job.kind.split(':')[1]] }}：{{
      (
        {
          queued: '排队中',
          running: '预热中…',
          complete: '预热完成',
          error: '预热失败',
          cancelled: '已取消',
        } as any
      )[job.status]
    }}
    {{ job.error || '' }}
  </p>
  <AppNotification :message="error" />
  <div class="actions">
    <button v-if="!submitted && available.length" :disabled="pending" @click="warmup.start(available)">
      {{ pending ? '正在提交…' : '现在预热' }}
    </button>
    <button :disabled="pending" @click="$emit('close')">
      {{ submitted ? (running ? '后台继续' : '完成') : available.length ? '暂不预热' : '稍后处理' }}
    </button>
    <button :disabled="pending" @click="$emit('settings')">打开设置</button>
  </div>
</template>
