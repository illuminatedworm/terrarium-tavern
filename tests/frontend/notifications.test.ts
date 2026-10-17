import { afterEach, expect, it, vi } from 'vitest';
import { notification, notify } from '../../web/lib/notifications';
import { createRenderer, defineComponent, nextTick, ref } from 'vue';
import { useStartupWarmup } from '../../web/composables/useStartupWarmup';

const request = vi.hoisted(() => vi.fn());
vi.mock('../../web/lib/api', () => ({ api: request }));
const renderer = createRenderer<any, any>({
  patchProp() {},
  insert() {},
  remove() {},
  createElement: () => ({}),
  createText: () => ({}),
  createComment: () => ({}),
  setText() {},
  setElementText() {},
  parentNode: () => null,
  nextSibling: () => null,
});
const apps: ReturnType<typeof renderer.createApp>[] = [];
function mountWarmup() {
  const open = ref(true);
  let warmup!: ReturnType<typeof useStartupWarmup>;
  const app = renderer.createApp(
    defineComponent({
      setup() {
        warmup = useStartupWarmup(() => open.value);
        return () => null;
      },
    }),
  );
  app.mount({});
  apps.push(app);
  return { open, warmup };
}
afterEach(() => {
  for (const app of apps.splice(0)) app.unmount();
  request.mockReset();
  vi.clearAllTimers();
  vi.useRealTimers();
  notification.value = '';
});
it('replaces the visible notification and dismisses eight seconds after the latest message', () => {
  vi.useFakeTimers();
  notify('first');
  vi.advanceTimersByTime(7000);
  notify('second');
  vi.advanceTimersByTime(1000);
  expect(notification.value).toBe('second');
  vi.advanceTimersByTime(7000);
  expect(notification.value).toBe('');
});

it('keeps observing after the startup dialog closes and notifies once per completed model', async () => {
  vi.useFakeTimers();
  const embedding = { id: 'embedding-job', kind: 'warmup:embedding', status: 'running' };
  const reranker = { id: 'reranker-job', kind: 'warmup:reranker', status: 'running' };
  let jobs = [embedding, reranker];
  request.mockImplementation(async (url: string) =>
    url === '/local-models'
      ? { jobs: [...jobs, { id: 'other-job', kind: 'download:embedding', status: 'complete' }] }
      : { ...(url.includes('/embedding/') ? embedding : reranker), status: 'queued' },
  );
  const { open, warmup } = mountWarmup();
  expect(request).not.toHaveBeenCalled();
  await warmup.start(['embedding', 'reranker']);
  open.value = false;
  await nextTick();

  jobs = [{ ...embedding, status: 'complete' }, reranker];
  await vi.advanceTimersByTimeAsync(1000);
  expect(notification.value).toBe('Embedding 预热完成');
  expect(warmup.running.value).toBe(true);
  notification.value = '';
  await vi.advanceTimersByTimeAsync(1000);
  expect(notification.value).toBe('');

  jobs = jobs.map((job) => ({ ...job, status: 'complete' }));
  await vi.advanceTimersByTimeAsync(1000);
  expect(notification.value).toBe('Reranker 预热完成');
  expect(warmup.running.value).toBe(false);
  const calls = request.mock.calls.length;
  await vi.advanceTimersByTimeAsync(8000);
  expect(notification.value).toBe('');
  expect(request).toHaveBeenCalledTimes(calls);
});

it('keeps completion in the open dialog and ignores an observation arriving after app disposal', async () => {
  vi.useFakeTimers();
  const embedding = { id: 'embedding-job', kind: 'warmup:embedding', status: 'complete' };
  const reranker = { id: 'reranker-job', kind: 'warmup:reranker', status: 'running' };
  request
    .mockResolvedValueOnce({ ...embedding, status: 'queued' })
    .mockResolvedValueOnce({ ...reranker, status: 'queued' })
    .mockResolvedValueOnce({ jobs: [embedding, reranker] });
  const { warmup } = mountWarmup();
  await warmup.start(['embedding', 'reranker']);
  expect(warmup.jobs.value[0].status).toBe('complete');
  expect(notification.value).toBe('');

  let finish!: (value: any) => void;
  request.mockImplementationOnce(() => new Promise((resolve) => (finish = resolve)));
  await vi.advanceTimersByTimeAsync(1000);
  apps.pop()!.unmount();
  finish({ jobs: [embedding, { ...reranker, status: 'complete' }] });
  await nextTick();
  expect(warmup.jobs.value[1].status).toBe('running');
  expect(notification.value).toBe('');
  expect(vi.getTimerCount()).toBe(0);
});

it('tracks submissions that finish after closing and combines simultaneous completions', async () => {
  vi.useFakeTimers();
  const finishes: ((value: any) => void)[] = [];
  request.mockImplementation(() => new Promise((resolve) => finishes.push(resolve)));
  const { open, warmup } = mountWarmup();
  const starting = warmup.start(['embedding', 'reranker']);
  open.value = false;
  await nextTick();
  finishes[0]({ id: 'embedding-job', kind: 'warmup:embedding', status: 'complete' });
  finishes[1]({ id: 'reranker-job', kind: 'warmup:reranker', status: 'complete' });
  await starting;
  expect(notification.value).toBe('Embedding 预热完成；Reranker 预热完成');
  expect(request).toHaveBeenCalledTimes(2);
  expect(warmup.running.value).toBe(false);
});
