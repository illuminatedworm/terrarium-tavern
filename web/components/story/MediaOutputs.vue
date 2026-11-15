<script setup lang="ts">
import { computed, inject, onBeforeUnmount, ref, watch } from 'vue';
import { mediaStatus, mediaUrl, storyMediaKey, type MediaJob } from '../../lib/media';

const props = defineProps<{ messageId: string }>();
const media = inject(storyMediaKey, null);
const allJobs = computed(() => media?.jobs.value[props.messageId] || []);
const showHistory = ref(false);
const currentJobs = computed(() => {
  const roots = new Map<string, string>();
  const groups = new Map<string, { latest: MediaJob; completed?: MediaJob }>();
  for (const job of allJobs.value) {
    const root = (job.previous && roots.get(job.previous)) || job.id;
    roots.set(job.id, root);
    const group = groups.get(root) || { latest: job };
    group.latest = job;
    if (job.status === 'completed') group.completed = job;
    groups.set(root, group);
  }
  return [...groups.values()].flatMap(({ latest, completed }) =>
    completed && completed.id !== latest.id ? [completed, latest] : [latest],
  );
});
const jobs = computed(() => (showHistory.value ? allJobs.value : currentJobs.value));
const audio = new Map<string, HTMLAudioElement>();
const playlist = ref<string[]>([]);
const running = ref(new Set<string>());
const error = ref('');
const preview = ref<HTMLDialogElement>();
const picture = ref('');
let unregister: (() => void) | undefined;
watch(
  () => props.messageId,
  (id) => {
    unregister?.();
    unregister = media?.register(id);
  },
  { immediate: true },
);
onBeforeUnmount(() => {
  unregister?.();
  audio.forEach((element) => element.pause());
});
const playable = computed(() => currentJobs.value.filter((job) => job.kind === 'speech' && job.output));
async function playNext() {
  const id = playlist.value[0];
  if (!id) return;
  try {
    await audio.get(id)?.play();
  } catch (failure) {
    error.value = String(failure);
    playlist.value = [];
  }
}
function playAll() {
  playlist.value = playable.value.map((job) => job.id);
  void playNext();
}
function ended(id: string) {
  if (playlist.value[0] === id) {
    playlist.value.shift();
    void playNext();
  }
}
async function action(job: MediaJob, kind: 'retry' | 'regenerate' | 'cancel') {
  running.value.add(job.id);
  error.value = '';
  try {
    await media?.action(job, kind);
  } catch (failure) {
    error.value = String(failure);
  } finally {
    running.value.delete(job.id);
  }
}
function showImage(url: string) {
  picture.value = url;
  preview.value?.showModal();
}
</script>

<template>
  <section v-if="jobs.length || media?.error.value" class="message-media" aria-label="配音与插图">
    <p v-if="error || media?.error.value" class="media-error" role="alert">
      {{ error || media?.error.value }} <button class="quiet" @click="media?.reload()">刷新媒体</button>
    </p>
    <button v-if="playable.length > 1" class="quiet" @click="playAll">按顺序播放本轮配音</button>
    <button v-if="allJobs.length > currentJobs.length" class="quiet" @click="showHistory = !showHistory">
      {{ showHistory ? '收起历史产物' : '查看历史产物与尝试' }}
    </button>
    <article v-for="job in jobs" :key="job.id" class="media-item">
      <header>
        <strong>{{ job.kind === 'speech' ? job.request.speaker : '剧情插图' }}</strong>
        <small
          >{{ job.kind === 'speech' ? 'AI 配音 · ' : '' }}{{ job.previous ? '重新生成 · ' : ''
          }}{{ mediaStatus[job.status] }}</small
        >
      </header>
      <template v-if="job.output">
        <audio
          v-if="job.kind === 'speech'"
          :ref="
            (el) => {
              if (el) audio.set(job.id, el as HTMLAudioElement);
              else audio.delete(job.id);
            }
          "
          :src="mediaUrl(job.output)"
          controls
          preload="none"
          aria-label="角色配音"
          @play="media?.playing($event.target as HTMLAudioElement)"
          @ended="ended(job.id)"
        />
        <button v-else class="media-picture" title="查看完整图片" @click="showImage(mediaUrl(job.output))">
          <img :src="mediaUrl(job.output)" :alt="job.request.prompt" loading="lazy" />
        </button>
      </template>
      <p v-if="job.error" role="alert" class="media-error">{{ job.error }}</p>
      <details>
        <summary>{{ job.kind === 'speech' ? '实际配音文本与设置' : '提示词、参考图与设置' }}</summary>
        <p class="media-text">{{ job.request.text ?? job.request.prompt }}</p>
        <p v-if="job.request.delivery">演绎：{{ job.request.delivery }}</p>
        <p v-if="job.kind === 'speech'" class="media-text">
          实际语音指令：{{
            [job.request.connection.instructions, job.request.delivery].filter(Boolean).join('\n\n')
          }}
        </p>
        <p>{{ job.request.connection.provider_name }} · {{ job.request.connection.model }}</p>
        <p>
          {{ job.request.connection.base_url }}/{{ job.request.connection.api_path }} · 超时
          {{ job.request.connection.timeout_seconds }} 秒
        </p>
        <p v-if="job.kind === 'speech'">声音：{{ job.request.voice }} · AI 生成语音</p>
        <p v-else>尺寸：{{ job.request.size }} · 画质：{{ job.request.connection.quality }}</p>
        <p v-for="reference in job.request.reference_names" :key="reference.ref">
          参考：{{ reference.ref }} · {{ reference.name }}
        </p>
        <small>使用任务创建时的设置；重试保持原参数，重新生成使用当前设置。</small>
      </details>
      <div class="media-actions">
        <button
          v-if="job.status === 'queued' || job.status === 'running'"
          class="quiet"
          :disabled="running.has(job.id)"
          @click="action(job, 'cancel')"
        >
          取消
        </button>
        <template v-else>
          <button
            v-if="job.status !== 'completed'"
            class="quiet"
            :disabled="running.has(job.id)"
            @click="action(job, 'retry')"
          >
            按原参数重试
          </button>
          <button class="quiet" :disabled="running.has(job.id)" @click="action(job, 'regenerate')">
            按当前设置重新生成
          </button>
        </template>
      </div>
    </article>
  </section>
  <Teleport to="body"
    ><dialog ref="preview" class="media-preview" @click="$event.target === preview && preview?.close()">
      <button class="quiet" autofocus @click="preview?.close()">关闭图片</button>
      <img v-if="picture" :src="picture" alt="剧情插图完整预览" /></dialog
  ></Teleport>
</template>

<style scoped>
.message-media {
  display: grid;
  gap: 0.65rem;
  margin-block: 1rem;
  font-size: var(--fs-sm);
}
.media-item {
  min-width: 0;
  padding: 0.7rem;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}
.media-item header,
.media-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.6rem;
}
.media-item header {
  margin-bottom: 0.5rem;
}
.media-item audio {
  display: block;
  width: min(100%, 28rem);
  margin-block: 0.5rem;
}
.media-picture {
  display: block;
  max-width: 100%;
  padding: 0;
  overflow: hidden;
}
.media-picture img {
  display: block;
  width: auto;
  max-width: 100%;
  max-height: 24rem;
  object-fit: contain;
}
.media-text {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.media-item details {
  margin-block: 0.5rem;
  overflow-wrap: anywhere;
}
.media-error {
  color: var(--danger);
  overflow-wrap: anywhere;
}
.media-preview {
  width: min(90vw, 80rem);
  height: min(85dvh, 55rem);
  max-height: 90dvh;
  padding: 1rem;
  background: var(--surface-panel);
  color: var(--text-primary);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
}
.media-preview[open] {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}
.media-preview img {
  flex: 1;
  min-height: 0;
  width: 100%;
  object-fit: contain;
}
.media-preview button {
  align-self: flex-end;
}
.media-preview::backdrop {
  background: rgb(0 0 0 / 0.65);
}
</style>
