import { ref } from 'vue';
import { rawApi } from './api';
import { notify } from './notifications';
import type { ModelProfile } from './settingsDocument';

export interface NovelConfig {
  model: ModelProfile | null;
  official_model: ModelProfile | null;
  max_requests: number;
  timeout_seconds: number;
}
export interface NovelSource {
  id: string;
  name: string;
  encoding: string;
  bytes: number;
  characters: number;
  created: number;
  missing: boolean;
  location: string;
  current: boolean;
}
export type NovelStatus = 'running' | 'waiting' | 'completed' | 'paused' | 'failed' | 'destroyed';
export interface NovelJob {
  id: string;
  ref: string;
  source: string;
  task: string;
  background: string;
  status: NovelStatus;
  pending_messages: number;
  delivered_messages: number;
  round_position: number;
  config: {
    profile: ModelProfile;
    follows_main: boolean;
    channel: 'standard' | 'chatgpt';
    max_requests: number;
    timeout_seconds: number;
  };
  requests: number;
  elapsed: number;
  usage: Record<string, number>;
  error: string | null;
  created: number;
}
export interface NovelState {
  event_seq: number;
  revision: number;
  enabled: boolean;
  source: NovelSource | null;
  history: NovelSource[];
  jobs: NovelJob[];
  archive_before: number | null;
  active: number;
  effective: NovelJob['config'];
}
export interface NovelTrace {
  seq: number;
  created: number;
  message: { role: string; content: string; tool_calls?: unknown[]; _novel_compaction?: boolean };
}
export const novelStatus: Record<NovelStatus, string> = {
  running: '调查中',
  waiting: '等待主 agent 回答',
  completed: '已报告 · 待销毁或追问',
  paused: '已暂停',
  failed: '调查失败',
  destroyed: '已销毁',
};
export const novelActivity = ref<Array<{ session: string; title: string; ref: number; status: NovelStatus }>>(
  [],
);
export const novelChanges = ref<Record<string, number>>({});
export const novelConnectionError = ref('');
export const novelConnectionEpoch = ref(0);
let retryObserver: (() => Promise<void>) | undefined;
export async function retryNovelActivity() {
  await retryObserver?.();
}

/** One reconnectable observer for every branch; no background status polling. */
export function observeNovels() {
  let source: EventSource | null = null;
  let disposed = false,
    refreshing = false,
    again = false,
    initialized = false,
    cursor = 0;
  async function refresh() {
    if (refreshing) {
      again = true;
      return;
    }
    refreshing = true;
    try {
      do {
        again = false;
        const result = await rawApi('/novel/activity');
        if (disposed) return;
        const prior = new Map(novelActivity.value.map((job) => [`${job.session}:${job.ref}`, job.status]));
        const failures = (result.jobs as typeof novelActivity.value).filter(
          (job) => job.status === 'failed' && prior.get(`${job.session}:${job.ref}`) !== 'failed',
        );
        if (initialized && failures.length)
          notify(
            `原著调查失败：${failures.map((job) => `${job.title || '对话'} · R${job.ref}`).join('、')}。请在原著面板查看原因及恢复或销毁。`,
          );
        novelActivity.value = result.jobs;
        initialized = true;
        if (!source) {
          cursor = result.cursor;
          source = new EventSource(`/api/novel/events?after=${cursor}`);
          source.onmessage = (message) => {
            if (disposed) return;
            const event = JSON.parse(message.data) as { seq: number; session: string };
            if (event.seq <= cursor) return;
            cursor = event.seq;
            novelChanges.value = { ...novelChanges.value, [event.session]: cursor };
            void refresh();
          };
          source.onopen = () => {
            novelConnectionError.value = '';
            novelConnectionEpoch.value++;
            void refresh();
          };
          source.onerror = () => {
            novelConnectionError.value = '调查状态连接中断，正在重连；调查不会因此停止。';
          };
          source.addEventListener('end', () => source?.close());
          source.addEventListener('refresh', () => {
            if (!disposed) void refresh();
          });
        }
      } while (again && !disposed);
    } catch (error) {
      if (!disposed) novelConnectionError.value = String(error);
    } finally {
      refreshing = false;
    }
  }
  retryObserver = refresh;
  void refresh();
  return () => {
    disposed = true;
    source?.close();
    if (retryObserver === refresh) retryObserver = undefined;
  };
}
