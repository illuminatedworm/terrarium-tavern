import { expect, it } from 'vitest';
import { createSessionController, StaleView } from '../../web/lib/sessionController';

it('rejects late mutations before they can overwrite the new session', async () => {
  let complete: (value: unknown) => void = () => {};
  const controller = createSessionController(
    () =>
      new Promise((resolve) => {
        complete = resolve;
      }),
  );
  controller.enter();
  const request = controller.request('/sessions/old/options', {}, 'PUT');
  const rejected = expect(request).rejects.toBeInstanceOf(StaleView);
  controller.enter();
  complete({ id: 'old' });
  await rejected;
});

it('accepts a response that still belongs to its originating view', async () => {
  const controller = createSessionController(async () => ({ id: 'current' }));
  controller.enter();
  expect(await controller.request('/sessions/current')).toEqual({ id: 'current' });
});
