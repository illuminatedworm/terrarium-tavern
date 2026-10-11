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
