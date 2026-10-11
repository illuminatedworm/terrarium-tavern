import { createRenderer, defineComponent, nextTick, ref } from 'vue';
import { describe, expect, it, vi } from 'vitest';
import { useContextOperations } from '../../web/composables/useContextOperations';

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
describe('context operation completion', () => {
  it('keeps controls locked until the completed operation has installed its new revision', async () => {
    const session = ref({ id: 'story', channel_origin: 'chatgpt', agent_mode: 'half_agent', revision: 1 });
    let finish!: (value: any) => void;
    let operations!: ReturnType<typeof useContextOperations>;
    request.mockReset();
    request.mockImplementation((url: string) =>
      url.endsWith('context-operation')
        ? Promise.resolve({ id: 'op', status: 'completed' })
        : new Promise((resolve) => {
            finish = resolve;
          }),
    );
    const app = renderer.createApp(
      defineComponent({
        setup() {
          operations = useContextOperations(
            () => session.value,
            (value) => {
              session.value = value;
            },
          );
          return () => null;
        },
      }),
    );
    app.mount({});
    await nextTick();
    expect(operations.busy.value).toBe(true);
    await operations.start('enable_local');
    expect(request).toHaveBeenCalledTimes(2);
    finish({ ...session.value, revision: 2, context_strategy: 'openai_compact' });
    await nextTick();
    expect(operations.busy.value).toBe(false);
    expect(session.value.revision).toBe(2);
    app.unmount();
  });

  it.each(['full_agent', 'half_agent'])(
    'refreshes an immediately completed %s switch without a polling loop',
    async (mode) => {
      const session = ref({
        id: 'short-story',
        channel_origin: 'chatgpt',
        agent_mode: mode,
        revision: 1,
      });
      let operations!: ReturnType<typeof useContextOperations>;
      request.mockReset();
      request.mockImplementation(async (url: string) => {
        if (url.endsWith('context-operation')) return null;
        if (url.endsWith('context-operations'))
          return { id: 'direct-switch', status: 'completed', converted: false };
        return { ...session.value, revision: 2, context_strategy: 'openai_compact' };
      });
      const app = renderer.createApp(
        defineComponent({
          setup() {
            operations = useContextOperations(
              () => session.value,
              (value) => {
                session.value = value;
              },
            );
            return () => null;
          },
        }),
      );
      app.mount({});
      await nextTick();
      await operations.start('enable_compact');
      expect(session.value.revision).toBe(2);
      expect(operations.busy.value).toBe(false);
      expect(request.mock.calls.map((call) => call[0])).toEqual([
        '/sessions/short-story/context-operation',
        '/sessions/short-story/context-operations',
        '/sessions/short-story',
      ]);
      app.unmount();
    },
  );

  it('keeps a late Half Agent result from replacing the newly selected session', async () => {
    const session = ref({ id: 'old', channel_origin: 'chatgpt', agent_mode: 'half_agent', revision: 1 });
    let complete!: (value: any) => void;
    request.mockReset();
    request.mockImplementation((url: string) => {
      if (url === '/sessions/old/context-operation')
        return new Promise((resolve) => {
          complete = resolve;
        });
      if (url.endsWith('context-operation')) return Promise.resolve(null);
      throw new Error('Stale operation must not fetch or replace a session');
    });
    let operations!: ReturnType<typeof useContextOperations>;
    const updated = vi.fn();
    const app = renderer.createApp(
      defineComponent({
        setup() {
          operations = useContextOperations(() => session.value, updated);
          return () => null;
        },
      }),
    );
    app.mount({});
    session.value = { ...session.value, id: 'new' };
    await nextTick();
    complete({ id: 'old-op', status: 'completed' });
    await nextTick();
    expect(updated).not.toHaveBeenCalled();
    expect(operations.operation.value).toBeNull();
    expect(operations.busy.value).toBe(false);
    app.unmount();
  });

  it('refreshes a completed session once without restarting polling on equivalent identity', async () => {
    const session = ref({ id: 'story', channel_origin: 'chatgpt', agent_mode: 'full_agent', revision: 1 });
    request.mockReset();
    request.mockImplementation(async (url: string) => {
      if (request.mock.calls.length > 8) throw new Error('Unexpected polling loop');
      return url.endsWith('context-operation')
        ? { id: 'operation', status: 'completed' }
        : { ...session.value, revision: 2 };
    });
    const app = renderer.createApp(
      defineComponent({
        setup() {
          useContextOperations(
            () => session.value,
            (value) => {
              session.value = value;
            },
          );
          return () => null;
        },
      }),
    );
    app.mount({});
    for (let i = 0; i < 6; i++) await nextTick();
    expect(session.value.revision).toBe(2);
    expect(request.mock.calls.map((call) => call[0])).toEqual([
      '/sessions/story/context-operation',
      '/sessions/story',
    ]);
    app.unmount();
  });
});
