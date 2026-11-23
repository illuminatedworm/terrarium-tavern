import { expect, it, vi } from 'vitest';
import { createRuntimeLoader } from '../../web/lib/storyRuntime';

it('shares runtime downloads across concurrent frames and later story opens', async () => {
  const request = vi.fn(async (url) => new Response(String(url)));
  const load = createRuntimeLoader(request);
  const frames = await Promise.all(Array.from({ length: 30 }, () => load()));
  expect(request).toHaveBeenCalledTimes(1);
  expect(frames[0]).toEqual('/static/runtime.js');
  expect(await load()).toEqual(frames[0]);
  expect(request).toHaveBeenCalledTimes(1);
  expect(await load('core')).toEqual('/static/mvu-core.js');
  expect(await load('core')).toEqual('/static/mvu-core.js');
  expect(request).toHaveBeenCalledTimes(2);
});

it('allows a later attempt after a failed download', async () => {
  const request = vi.fn(async () => new Response('sdk'));
  request.mockResolvedValueOnce(new Response('unavailable', { status: 503 }));
  const load = createRuntimeLoader(request);
  await expect(load()).rejects.toThrow('503');
  expect(await load()).toEqual('sdk');
  expect(request).toHaveBeenCalledTimes(2);
});

it('scores the retained physical faces when a computed pool and a penetrating die are rerolled', async () => {
  const { diceSession } = await vi.importActual<{
    diceSession(summary: boolean, random: () => number): (formula: string) => unknown;
  }>('../../compat/calculation/dice.js');
  const faces = [2, 4, 1, 3, 6, 2, 5];
  const roll = diceSession(false, () => (faces.shift()! - 0.5) / 6);
  const formula = '0d6 + (3/2)d6r1r2 + 1d6!pro<2 * -1.5 - 0.5';
  expect(roll(formula)).toEqual({
    formula,
    total: -8.5,
    pools: [
      { sides: 6, dice: [], total: 0 },
      { sides: 6, dice: [{ value: 3, rerolled: [2, 1] }, { value: 4 }], total: 7 },
      { sides: 6, dice: [{ value: 6 }, { extra: true, face: 5, value: 4, rerolled: [2] }], total: 10 },
    ],
  });
  expect(faces).toHaveLength(0);
});
