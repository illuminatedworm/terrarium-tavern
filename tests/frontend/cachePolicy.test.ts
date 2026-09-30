import { describe, expect, it } from 'vitest';
import { cachePreset, cacheRetention, modelEndpoint, type CacheSettings } from '../../web/lib/cachePolicy';
import { difference } from '../../web/lib/settingsDocument';

const cache: CacheSettings = {
  mode: 'runtime',
  preset: 'auto',
  field: 'cache_control',
  value: { type: 'ephemeral' },
  request: {},
};

describe('cache configuration', () => {
  it('offers protocol-specific retention tiers and the lowest default after switching models', () => {
    expect(cacheRetention('claude-opus-5-5', 'anthropic', cache).options.map((o) => o.value)).toEqual([
      '5m',
      '1h',
    ]);
    expect(cacheRetention('gemini-3.8-flash', 'gemini', cache).options.map((o) => o.value)).toEqual([
      '5m',
      '15m',
      '1h',
      '24h',
    ]);
    expect(cacheRetention('gpt-5.4', 'responses', cache).value).toBe('in_memory');
    expect(cacheRetention('gpt-5.5', 'responses', cache).options.map((o) => o.value)).toEqual(['24h']);
    expect(cacheRetention('gpt-6-sol', 'responses', { ...cache, retention: '1h' }).value).toBe('30m');
    expect(cacheRetention('unknown', 'responses', cache).enabled).toBe(false);
    expect(cacheRetention('claude', 'responses', cache).enabled).toBe(false);
    expect(cacheRetention('claude', 'anthropic', { ...cache, mode: 'cloud' }).enabled).toBe(true);
    expect(cacheRetention('claude', 'chat_completions', { ...cache, mode: 'cloud' }).enabled).toBe(false);
    expect(cacheRetention('gpt-5.4', 'chat_completions', { ...cache, mode: 'cloud' }).enabled).toBe(true);
    expect(cachePreset('claude', 'anthropic', { ...cache, retention: '1h' }).value).toEqual({
      type: 'ephemeral',
      ttl: '1h',
    });
    expect(cachePreset('gpt-6-sol', 'responses', cache).request).toEqual({
      prompt_cache_options: { mode: 'explicit', ttl: '30m' },
    });
  });
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
