import type { ContextUsage } from './contextUsage';

export interface ConversationCacheUsage {
  prompt_tokens: number | null;
  cached_tokens: number | null;
}

export interface EditorContextStats {
  context_usage?: ContextUsage;
  context_limit: number;
  model: string;
  usage?: Record<string, any> | null;
  cache_usage?: ConversationCacheUsage | null;
  image_count?: number;
}

export const conversationCacheRateTitle =
  '缓存命中率为当前会话累计缓存读取 ÷ 累计输入 tokens（输入含命中部分），仅按接口实际用量统计。';

/** The server aggregates provider counters; never substitute the last response. */
export function conversationCacheRatio(usage?: ConversationCacheUsage | null): number | null {
  if (!usage || usage.prompt_tokens == null || usage.cached_tokens == null || usage.prompt_tokens <= 0)
    return null;
  return usage.cached_tokens / usage.prompt_tokens;
}

export function conversationCacheRate(usage?: ConversationCacheUsage | null): string {
  const ratio = conversationCacheRatio(usage);
  return ratio == null ? '暂无' : `${Math.round(ratio * 100)}%`;
}
