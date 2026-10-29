import { computed, onBeforeUnmount, ref } from 'vue';
import { rawApi } from '../lib/api';
import type { ContextUsage } from '../lib/contextUsage';
import {
  advisorRunning,
  eventCursor,
  type AdvisorEvent,
  type AdvisorRun,
  type AdvisorState,
} from '../lib/advisor';

export function useAdvisor(sid: string, preferenceSaved: () => void, started: () => void = () => {}) {
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
    const token = generation;
    const value = (await rawApi(`/sessions/${sid}/advisor`)) as AdvisorState;
    if (disposed || token !== generation || clearing.value) return;
    state.value = value;
    if (observe && advisorRunning(value.run)) watchRun(value.run!);
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
    const observer = new EventSource(`/api/advisor-runs/${run.id}/events`);
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
        if (event.payload.preference_updated) preferenceSaved();
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
      const value = await rawApi(
        `/sessions/${sid}/advisor/options`,
        { expected_revision: state.value.revision, ...changes },
        'PATCH',
      );
      if (!disposed && token === generation) {
        state.value = value;
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
      const value = await rawApi(`/sessions/${sid}/advisor/discard`, {
        expected_revision: state.value.revision,
      });
      if (disposed || token !== generation) return;
      state.value = value;
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
      const value = await rawApi(`/sessions/${sid}/advisor/clear`, {
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
