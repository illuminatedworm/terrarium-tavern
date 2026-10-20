import { afterEach, expect, it, vi } from 'vitest';
import { api, rawApi, streamApi } from '../../web/lib/api';

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

it('delivers a final NDJSON record that lacks a trailing newline', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response('{"n":1}\n{"n":2}')),
  );
  const lines: unknown[] = [];
  await streamApi('/stream', {}, new AbortController().signal, (line) => lines.push(line));
  expect(lines).toEqual([{ n: 1 }, { n: 2 }]);
});
