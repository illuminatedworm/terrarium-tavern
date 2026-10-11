import { describe, expect, it } from 'vitest';
import { editorCacheRate } from '../../web/lib/editorContextStats';

describe('editor cache hit rate', () => {
  it('uses the provider-specific input denominator', () => {
    expect(editorCacheRate({ prompt_tokens: 1000, prompt_tokens_details: { cached_tokens: 750 } })).toBe(
      '75%',
    );
    expect(editorCacheRate({ input_tokens: 1000, input_tokens_details: { cached_tokens: 250 } })).toBe('25%');
    expect(
      editorCacheRate({ input_tokens: 100, cache_read_input_tokens: 800, cache_creation_input_tokens: 100 }),
    ).toBe('80%');
    expect(editorCacheRate({ promptTokenCount: 1000, cachedContentTokenCount: 600 })).toBe('60%');
  });
  it('distinguishes unknown counters from zero hits', () => {
    expect(editorCacheRate(null)).toBe('暂无数据');
    expect(editorCacheRate({})).toBe('服务商未提供');
    expect(editorCacheRate({ prompt_tokens: 1000 })).toBe('服务商未提供');
    expect(editorCacheRate({ prompt_tokens: 1000, cached_tokens: 0 })).toBe('0%');
    expect(editorCacheRate({ cached_tokens: 40 })).toBe('服务商未提供');
  });
});
