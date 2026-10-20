import { describe, expect, it } from 'vitest';
import { conversationCacheRate } from '../../web/lib/editorContextStats';

describe('conversation cache hit rate', () => {
  it('formats cumulative provider totals and keeps missing data distinct from real zero hits', () => {
    // 75/100 and 225/900 are 300/1000 overall, not the last 25% or an average 50%.
    expect(conversationCacheRate({ prompt_tokens: 1000, cached_tokens: 300 })).toBe('30%');
    expect(conversationCacheRate({ prompt_tokens: 1000, cached_tokens: 0 })).toBe('0%');
    expect(conversationCacheRate()).toBe('暂无');
    expect(conversationCacheRate(null)).toBe('暂无');
    expect(conversationCacheRate({ prompt_tokens: 1000, cached_tokens: null })).toBe('暂无');
    expect(conversationCacheRate({ prompt_tokens: null, cached_tokens: 40 })).toBe('暂无');
    expect(conversationCacheRate({ prompt_tokens: 0, cached_tokens: 0 })).toBe('暂无');
  });
});
