import { describe, expect, it } from 'vitest';
import { cachePreset, modelEndpoint, type CacheSettings } from '../../web/lib/cachePolicy';
import { difference } from '../../web/lib/settingsDocument';

const cache: CacheSettings = {
  mode: 'runtime',
  preset: 'auto',
  field: 'cache_control',
  value: { type: 'ephemeral' },
  request: {},
};

describe('cache configuration', () => {
  it('matches protocols and preserves explicit custom values', () => {
    expect(cachePreset('claude-opus-4-6', 'chat_completions', cache).field).toBe('cache_control');
    expect(cachePreset('gpt-5.6-sol', 'responses', cache).field).toBe('prompt_cache_breakpoint');
    expect(cachePreset('gemini-3.8-flash-high', 'chat_completions', cache).kind).toBe('gemini');
    expect(cachePreset('renamed-model', 'gemini', cache).kind).toBe('gemini');
    expect(cachePreset('gemini-alias', 'anthropic', cache).field).toBe('cache_control');
    expect(cachePreset('unknown', 'chat_completions', cache).kind).toBe('unknown');
    expect(
      cachePreset('claude', 'anthropic', { ...cache, preset: 'custom', field: 'custom_cache', value: false }),
    ).toEqual({ kind: 'block', field: 'custom_cache', value: false });
  });
  it('patches arbitrary JSON objects atomically, including key removal', () => {
    expect(
      difference(
        { cache },
        {
          cache: {
            ...cache,
            value: { mode: 'explicit' },
            request: { prompt_cache_options: { mode: 'explicit' } },
          },
        },
      ),
    ).toEqual({
      'cache.value': { mode: 'explicit' },
      'cache.request': { prompt_cache_options: { mode: 'explicit' } },
    });
    expect(difference({ cache: { ...cache, request: { prompt_cache_key: 'old' } } }, { cache })).toEqual({
      'cache.request': {},
    });
  });
  it('shows the actual protocol path without duplicating version prefixes', () => {
    expect(modelEndpoint('https://host.test', 'anthropic')).toBe('https://host.test/v1/messages');
    expect(modelEndpoint('https://host.test/v1', 'responses')).toBe('https://host.test/v1/responses');
    expect(modelEndpoint('https://host.test/custom/v1', 'chat_completions')).toBe(
      'https://host.test/custom/v1/chat/completions',
    );
    expect(modelEndpoint('https://host.test/custom/v1beta', 'gemini', 'gemini-3.8-flash-high')).toBe(
      'https://host.test/custom/v1beta/models/gemini-3.8-flash-high:generateContent',
    );
  });
});
