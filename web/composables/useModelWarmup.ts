import { computed, onBeforeUnmount, ref } from 'vue';
import { api } from '../lib/api';
import { notify } from '../lib/notifications';
import { useTimedError } from './useTimedError';

export type LocalModelPurpose = 'embedding' | 'reranker';
export interface WarmupJob {
  id: string;
  kind: string;
  status: 'queued' | 'running' | 'complete' | 'error' | 'cancelled';
  error?: string;
}
/** `GET /local-models`. */
export interface LocalModelStatus {
  startup_id: string;
  models: Record<
    LocalModelPurpose,
    { downloaded: boolean; ready: boolean; model: string; quantization: string }
  >;
  jobs: WarmupJob[];
}
/** The status the startup dialog was opened with, and the enabled models it offers to warm. */
export type StartupModels = LocalModelStatus & { enabled: LocalModelPurpose[] };
export const localModelLabels: Record<string, string> = {
  embedding: 'Embedding',
  reranker: 'Reranker',
};

/**
 * Every warmup — from the startup dialog or the local model settings — is submitted here.
 * The app owns observation so jobs keep being tracked after their dialog or page closes, and
 * completion is announced once unless the startup dialog is open to show it inline.
 */
export function useModelWarmup(isStartupOpen: () => boolean) {
  const jobs = ref<WarmupJob[]>([]);
  const submitting = ref(0),
    error = ref('');
  const pending = computed(() => submitting.value > 0);
  const running = computed(() => jobs.value.some((job) => ['queued', 'running'].includes(job.status)));
  let timer: ReturnType<typeof setTimeout> | undefined;
  let disposed = false;

  function updateJobs(updated: WarmupJob[]) {
    const notices: string[] = [];
    for (const job of updated) {
      if (isStartupOpen() || jobs.value.find((previous) => previous.id === job.id)?.status === job.status)
        continue;
      const label = localModelLabels[job.kind.split(':')[1]];
      if (job.status === 'complete') notices.push(`${label} 预热完成`);
      else if (job.status === 'error') notices.push(`${label} 预热失败：${job.error}`);
    }
    jobs.value = [...jobs.value.filter((job) => !updated.some((item) => item.id === job.id)), ...updated];
    if (notices.length) notify(notices.join('；'));
  }

  async function refresh() {
    // A new submission restarts polling; keep a single chain.
    clearTimeout(timer);
    try {
      const status = await api('/local-models');
      if (disposed) return;
      updateJobs(jobs.value.map((job) => status.jobs.find((item: WarmupJob) => item.id === job.id) || job));
      if (running.value) timer = setTimeout(refresh, 1000);
    } catch (e) {
      if (disposed) return;
      error.value = String(e);
      if (!isStartupOpen()) notify(error.value);
    }
  }

  /** Returns the jobs that were accepted so callers can show them immediately. */
  async function start(purposes: LocalModelPurpose[]) {
    submitting.value++;
    error.value = '';
    const results = await Promise.allSettled(
      purposes.map((purpose) => api(`/local-models/${purpose}/warmup`, {}) as Promise<WarmupJob>),
    );
    submitting.value--;
    if (disposed) return [];
    const accepted = results.flatMap((result) => (result.status === 'fulfilled' ? [result.value] : []));
    updateJobs(accepted);
    error.value = results
      .flatMap((result) => (result.status === 'rejected' ? [String(result.reason)] : []))
      .join('\n');
    if (error.value && !isStartupOpen()) notify(error.value);
    if (running.value) await refresh();
    return accepted;
  }

  onBeforeUnmount(() => {
    disposed = true;
    clearTimeout(timer);
  });
  useTimedError(error);
  return { jobs, pending, error, running, start };
}
export type ModelWarmupState = ReturnType<typeof useModelWarmup>;
