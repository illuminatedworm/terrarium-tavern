import { onBeforeUnmount, ref, watch } from 'vue';
import { api } from '../lib/api';
import type { MediaJob, StoryMedia } from '../lib/media';

/** One reconnectable observer per open story; load only mounted reply media. */
export function useStoryMedia(sessionId: () => string | undefined): StoryMedia {
  const jobs = ref<Record<string, MediaJob[]>>({});
  const error = ref('');
  const watched = new Map<string, number>();
  const pending = new Set<string>();
  let generation = 0;
  let queued = false;
  let events: EventSource | undefined;
  let activeAudio: HTMLAudioElement | undefined;
  let lastSeq = -1;
  const requests = new Set<AbortController>();

  function accept(job: MediaJob) {
    if (job.session !== sessionId() || !watched.has(job.message)) return;
    const items = jobs.value[job.message] || [];
    const old = items.find((item) => item.id === job.id);
    if (old && old.seq > job.seq) return;
    jobs.value[job.message] = [...items.filter((item) => item.id !== job.id), job].sort(
      (a, b) => a.ordinal - b.ordinal,
    );
  }
  function schedule() {
    if (queued) return;
    queued = true;
    queueMicrotask(() => {
      queued = false;
      void load();
    });
  }
  async function load() {
    const sid = sessionId();
    if (!sid || !pending.size) return;
    const current = generation;
    const ids = [...pending];
    pending.clear();
    const controller = new AbortController();
    requests.add(controller);
    try {
      for (let offset = 0; offset < ids.length; offset += 100) {
        const batch = ids.slice(offset, offset + 100);
        const result: MediaJob[] = await api(
          `/sessions/${sid}/media/jobs`,
          { message_ids: batch },
          'POST',
          controller.signal,
        );
        if (generation !== current) return;
        batch.forEach((id) => (jobs.value[id] ||= []));
        result.forEach(accept);
      }
      error.value = '';
    } catch (failure) {
      if (generation === current && !controller.signal.aborted) error.value = String(failure);
    } finally {
      requests.delete(controller);
    }
  }
  function reload() {
    watched.forEach((_, id) => pending.add(id));
    schedule();
  }
  watch(
    sessionId,
    (sid) => {
      generation++;
      events?.close();
      requests.forEach((request) => request.abort());
      activeAudio?.pause();
      activeAudio = undefined;
      jobs.value = {};
      pending.clear();
      watched.clear();
      error.value = '';
      lastSeq = -1;
      if (!sid) return;
      const current = generation;
      events = new EventSource(`/api/sessions/${sid}/media/events`);
      events.onopen = () => {
        if (current === generation) {
          lastSeq = -1;
          error.value = '';
        }
      };
      events.onmessage = (event) => {
        if (current !== generation) return;
        const update = JSON.parse(event.data) as { seq: number; reset?: boolean; job?: MediaJob };
        if (update.seq <= lastSeq) return;
        lastSeq = update.seq;
        if (update.reset) reload();
        if (update.job) accept(update.job);
      };
      events.onerror = () => {
        if (current === generation) error.value = '媒体进度连接中断，正在重新连接；可手动刷新。';
      };
    },
    { immediate: true },
  );
  onBeforeUnmount(() => {
    generation++;
    events?.close();
    requests.forEach((request) => request.abort());
    activeAudio?.pause();
  });
  return {
    jobs,
    error,
    reload,
    register(message) {
      watched.set(message, (watched.get(message) || 0) + 1);
      pending.add(message);
      schedule();
      return () => {
        const count = (watched.get(message) || 0) - 1;
        if (count > 0) watched.set(message, count);
        else {
          watched.delete(message);
          delete jobs.value[message];
        }
      };
    },
    playing(audio) {
      if (activeAudio !== audio) activeAudio?.pause();
      activeAudio = audio;
    },
    async action(job, action) {
      const updated: MediaJob = await api(`/sessions/${job.session}/media/jobs/${job.id}/${action}`, {
        operation_id: crypto.randomUUID(),
      });
      accept(updated);
    },
  };
}
