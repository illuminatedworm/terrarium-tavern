export interface ConversationCacheUsage {
  prompt_tokens: number | null;
  cached_tokens: number | null;
}

export interface EditorContextStats {
  estimated_tokens: number;
  context_limit: number;
  model: string;
  usage?: Record<string, any> | null;
  cache_usage?: ConversationCacheUsage | null;
  token_counting?: { tokenizer: string; source: string };
  image_count?: number;
  image_tokens_included?: boolean;
  estimated_tokens_complete?: boolean;
}

export const conversationCacheRateTitle =
  '当前会话累计缓存读取 tokens ÷ 累计输入 tokens（含缓存命中部分）；运行期间保留上次完整统计，结束后更新；仅统计接口真实用量，已完成请求缺少所需数据时显示“暂无”。';

/** The server aggregates provider counters; never substitute the last response. */
export function conversationCacheRate(usage?: ConversationCacheUsage | null): string {
  if (!usage || usage.prompt_tokens == null || usage.cached_tokens == null || usage.prompt_tokens <= 0)
    return '暂无';
  return `${Math.round((usage.cached_tokens / usage.prompt_tokens) * 100)}%`;
}
