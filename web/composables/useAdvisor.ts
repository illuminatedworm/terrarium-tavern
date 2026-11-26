import { computed, onBeforeUnmount, ref } from 'vue';
import { rawApi } from '../lib/api';
import type { ContextUsage } from '../lib/contextUsage';
import {
  advisorRunning,
  eventCursor,
  type AdvisorEvent,
  type AdvisorPreference,
  type AdvisorRun,
  type AdvisorState,
} from '../lib/advisor';

export function useAdvisor(
  sid: string,
  preferenceSaved: (before?: number | null, update?: AdvisorPreference) => void,
  started: () => void = () => {},
) {
  const state = ref<AdvisorState | null>(null),
    error = ref(''),
    loading = ref(false),
    sending = ref(false),
    clearing = ref(false);
  const streaming = ref(''),
    events = ref<AdvisorEvent[]>([]),
    notice = ref('');
  const stats = ref<{ context_usage: ContextUsage; context_limit: number } | null>(null),
    statsError = ref('');
  let source: EventSource | null = null,
    disposed = false,
    generation = 0,
    statsVersion = 0;
  const running = computed(() => sending.value || clearing.value || advisorRunning(state.value?.run));
  const pending = computed(() => !!state.value?.run?.resumable);
  let requestOrder = 0,
    lastApplied = 0;
  async function readState(path = '', data?: Record<string, unknown>, method?: string) {
    const token = generation,
      order = ++requestOrder;
    const previous = state.value?.exchanges ?? [];
    const after = previous.at(-1)?.seq ?? 0;
    const value: AdvisorState & { after: number; stale: number[] } = await rawApi(
      `/sessions/${sid}/advisor${path}?reader=true&after=${after}`,
      data,
      method,
    );
    if (
      disposed ||
      token !== generation ||
      (state.value &&
        (value.revision < state.value.revision ||
          (value.revision === state.value.revision && order < lastApplied)))
    )
      return false;
    if (value.after) {
      const stale = new Set(value.stale);
      value.exchanges = previous
        .map((row) => (row.stale === stale.has(row.seq) ? row : { ...row, stale: stale.has(row.seq) }))
        .concat(value.exchanges);
    }
    state.value = value;
    lastApplied = Math.max(lastApplied, order);
    return true;
  }
  function stopObserving() {
    generation++;
    source?.close();
    source = null;
  }
  async function refreshStats() {
    const token = generation,
      version = ++statsVersion;
    stats.value = null;
    statsError.value = '';
    if (!state.value?.effective.model_selection?.model || advisorRunning(state.value.run)) return;
    try {
      const value = await rawApi(`/sessions/${sid}/advisor/stats`);
      if (!disposed && token === generation && version === statsVersion) stats.value = value;
    } catch (e) {
      if (!disposed && token === generation && version === statsVersion) statsError.value = String(e);
    }
  }
  async function load(observe = true) {
    if (clearing.value || !(await readState())) return;
    if (observe && advisorRunning(state.value?.run)) watchRun(state.value!.run!);
    else await refreshStats();
  }
  function watchRun(run: AdvisorRun) {
    stopObserving();
    const token = generation,
      cursor = eventCursor();
    let reply = '',
      current = '';
    streaming.value = '';
    events.value = [];
    notice.value = '';
    const observer = new EventSource(`/api/advisor-runs/${run.id}/events?reader=true`);
    source = observer;
    observer.onmessage = (message) => {
      if (disposed || token !== generation) return;
      const event = JSON.parse(message.data) as AdvisorEvent;
      if (!cursor.accept(event)) return;
      // Prose from earlier responses stays part of the reply; a restart only
      // withdraws the unfinished response, as the server joins the same way.
      if (event.kind === 'response_started') {
        reply = event.payload.reply || '';
        current = '';
      } else if (event.kind === 'response_restarted') current = '';
      else if (event.kind === 'text') current += event.payload.text;
      else if (event.kind === 'context_warning' || event.kind === 'compacting')
        notice.value = event.payload.message;
      else if (event.kind === 'completed') {
        if (event.payload.preference_updated) preferenceSaved(run.story_revision, event.payload.story_update);
        notice.value = event.payload.preference_updated ? '本轮偏好已保存' : '';
      } else if (event.kind === 'error' || event.kind === 'cancelled') error.value = event.payload.message;
      streaming.value = current.trim() && reply ? `${reply}\n\n${current}` : reply || current;
      if (!['text', 'response_started'].includes(event.kind)) events.value.push(event);
    };
    observer.addEventListener('end', () => {
      if (token !== generation || disposed) return;
      stopObserving();
      void load(false).catch((e) => {
        error.value = String(e);
      });
    });
    observer.onerror = () => {
      if (!disposed && token === generation) notice.value = '连接中断，正在重新连接…';
    };
  }
  async function send(text: string, resume = false, compact = false) {
    if (!state.value || running.value) return false;
    error.value = '';
    streaming.value = '';
    events.value = [];
    sending.value = true;
    try {
      const run = (await rawApi(`/sessions/${sid}/advisor/runs`, {
        text,
        expected_revision: state.value.revision,
        operation_id: crypto.randomUUID(),
        resume_run_id: resume ? state.value.run?.id : null,
        compact,
      })) as AdvisorRun;
      if (disposed) return false;
      started();
      state.value.run = run;
      watchRun(run);
      return true;
    } catch (e) {
      error.value = String(e);
      return false;
    } finally {
      sending.value = false;
    }
  }
  async function options(changes: Record<string, unknown>) {
    if (!state.value || running.value) return;
    const token = generation;
    error.value = '';
    try {
      const accepted = await readState(
        '/options',
        { expected_revision: state.value.revision, ...changes },
        'PATCH',
      );
      if (accepted && !disposed && token === generation) {
        await refreshStats();
      }
    } catch (e) {
      error.value = String(e);
    }
  }
  async function cancel() {
    if (state.value?.run) await rawApi(`/advisor-runs/${state.value.run.id}/cancel`, {});
  }
  async function discard() {
    if (!state.value || running.value) return;
    const token = generation;
    try {
      const accepted = await readState('/discard', {
        expected_revision: state.value.revision,
      });
      if (!accepted || disposed || token !== generation) return;
      error.value = '';
      streaming.value = '';
      events.value = [];
      await refreshStats();
    } catch (e) {
      error.value = String(e);
    }
  }
  async function initialize() {
    loading.value = true;
    try {
      await load();
    } catch (e) {
      error.value = String(e);
    } finally {
      loading.value = false;
    }
  }
  async function clear() {
    if (!state.value || running.value) return false;
    clearing.value = true;
    stopObserving();
    try {
      const value = await rawApi(`/sessions/${sid}/advisor/clear?reader=true`, {
        expected_revision: state.value.revision,
      });
      if (disposed) return false;
      state.value = value;
      streaming.value = '';
      events.value = [];
      notice.value = '';
      error.value = '';
      await refreshStats();
      return true;
    } catch (e) {
      if (!disposed) error.value = String(e);
      return false;
    } finally {
      clearing.value = false;
    }
  }
  onBeforeUnmount(() => {
    disposed = true;
    stopObserving();
  });
  return {
    state,
    error,
    loading,
    running,
    pending,
    streaming,
    events,
    notice,
    stats,
    statsError,
    load,
    initialize,
    send,
    options,
    cancel,
    discard,
    clear,
    clearing,
  };
}
