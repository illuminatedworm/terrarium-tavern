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
