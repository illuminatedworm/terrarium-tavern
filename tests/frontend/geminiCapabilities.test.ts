import { describe, expect, it } from 'vitest';
import { geminiControls } from '../../web/lib/geminiCapabilities';
import { cachePreset, cacheRetention, type CacheSettings } from '../../web/lib/cachePolicy';

const legacy: CacheSettings = {
  mode: 'runtime',
  preset: 'custom',
  field: 'obsolete_marker',
  value: { type: 'ephemeral' },
  request: { obsolete_cache_option: true },
  retention: '24h',
};

describe('Gemini documented parameter capabilities', () => {
  it.each([
    ['gemini-2.5-flash', true, true],
    ['gemini-3.5-flash', true, true],
    ['gemini-3.5-flash-lite-preview', false, true],
    ['models/gemini-3.6-flash', false, true],
    ['gemini-3.8-flash-high', false, true],
    ['gemini-4-argon', false, false],
    ['custom-model', true, true],
  ] as const)('%s has matching generation and cache controls', (model, sampling, explicitCache) => {
    expect(geminiControls('gemini', model)).toEqual({ sampling, explicitCache });
  });

  it('keeps unrelated protocols and model aliases independent', () => {
    expect(geminiControls('anthropic', 'gemini-4-argon')).toEqual({ sampling: true, explicitCache: true });
    expect(geminiControls('responses', 'gemini-4-argon')).toEqual({ sampling: true, explicitCache: true });
    expect(geminiControls('gemini', 'not-gemini-4-argon')).toEqual({ sampling: true, explicitCache: true });
  });

  it('disables deprecated cache controls even for saved custom configuration without mutating it', () => {
    const saved = structuredClone(legacy);
    const policy = cacheRetention('gemini-4-argon', 'gemini', legacy);
    expect(policy.explicit).toBe(false);
    expect(policy.enabled).toBe(false);
    expect(policy.options).toEqual([]);
    expect(cachePreset('gemini-4-argon', 'gemini', legacy)).toEqual({ kind: 'implicit' });
    expect(cachePreset('gemini-3.8-flash', 'gemini', legacy)).toEqual({ kind: 'gemini' });
    expect(cacheRetention('gemini-3.8-flash', 'gemini', legacy).enabled).toBe(true);
    expect(legacy).toEqual(saved);
  });
});
