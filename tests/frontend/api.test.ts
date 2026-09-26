import { afterEach, expect, it, vi } from 'vitest';
import { api, rawApi } from '../../web/lib/api';

afterEach(() => vi.unstubAllGlobals());

it('retains exact debug payloads while dialogue hides manually removed lore', async () => {
  const payload = { before: { lore_injections: [{ id: 'old', manually_removed: true }, { id: 'active' }] } };
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify(payload))),
  );
  expect(await rawApi('/records')).toEqual(payload);
  expect((await api('/records')).before.lore_injections).toEqual([{ id: 'active' }]);
});
