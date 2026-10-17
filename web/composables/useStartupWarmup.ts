import { computed, onBeforeUnmount, ref } from 'vue';
import { api } from '../lib/api';
import { notify } from '../lib/notifications';
import { useTimedError } from './useTimedError';

export type LocalModelPurpose = 'embedding' | 'reranker';
interface WarmupJob {
  id: string;
  kind: string;
  status: 'queued' | 'running' | 'complete' | 'error' | 'cancelled';
  error?: string;
}
export const localModelLabels: Record<string, string> = {
  embedding: 'Embedding',
  reranker: 'Reranker',
};

/** The app owns observation so closing the startup dialog keeps submitted jobs visible. */
export function useStartupWarmup(isOpen: () => boolean) {
  const jobs = ref<WarmupJob[]>([]);
  const pending = ref(false),
    submitted = ref(false),
    error = ref('');
  const running = computed(() => jobs.value.some((job) => ['queued', 'running'].includes(job.status)));
  let timer: ReturnType<typeof setTimeout> | undefined;
  let disposed = false;

  function updateJobs(updated: WarmupJob[]) {
    const notices: string[] = [];
    for (const job of updated) {
      if (isOpen() || jobs.value.find((previous) => previous.id === job.id)?.status === job.status) continue;
      const label = localModelLabels[job.kind.split(':')[1]];
      if (job.status === 'complete') notices.push(`${label} 预热完成`);
      else if (job.status === 'error') notices.push(`${label} 预热失败：${job.error}`);
    }
    jobs.value = updated;
    if (notices.length) notify(notices.join('；'));
  }

  async function refresh() {
    try {
      const status = await api('/local-models');
      if (disposed) return;
      updateJobs(jobs.value.map((job) => status.jobs.find((item: WarmupJob) => item.id === job.id) || job));
      if (running.value) timer = setTimeout(refresh, 1000);
    } catch (e) {
      if (disposed) return;
      error.value = String(e);
      if (!isOpen()) notify(error.value);
    }
  }

  async function start(available: LocalModelPurpose[]) {
    if (pending.value || submitted.value) return;
    pending.value = true;
    error.value = '';
    const results = await Promise.allSettled(
      available.map((purpose) => api(`/local-models/${purpose}/warmup`, {})),
    );
    if (disposed) return;
    updateJobs(results.flatMap((result) => (result.status === 'fulfilled' ? [result.value] : [])));
    error.value = results
      .flatMap((result) => (result.status === 'rejected' ? [String(result.reason)] : []))
      .join('\n');
    if (error.value && !isOpen()) notify(error.value);
    submitted.value = true;
    pending.value = false;
    if (running.value) await refresh();
  }

  onBeforeUnmount(() => {
    disposed = true;
    clearTimeout(timer);
  });
  useTimedError(error);
  return { jobs, pending, submitted, error, running, start };
}
export type StartupWarmupState = ReturnType<typeof useStartupWarmup>;
