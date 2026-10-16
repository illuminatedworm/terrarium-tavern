export interface EditorContextStats {
  estimated_tokens: number;
  context_limit: number;
  model: string;
  usage?: Record<string, any> | null;
  token_counting?: { tokenizer: string; source: string };
  image_count?: number;
  image_tokens_included?: boolean;
  estimated_tokens_complete?: boolean;
}

/** Provider usage conventions differ; unknown counters are not zero hits. */
export function editorCacheRate(usage?: Record<string, any> | null): string {
  if (!usage) return '暂无数据';
  const cached =
    usage.prompt_tokens_details?.cached_tokens ??
    usage.input_tokens_details?.cached_tokens ??
    usage.cache_read_input_tokens ??
    usage.cached_tokens ??
    usage.cachedContentTokenCount;
  if (typeof cached !== 'number') return '服务商未提供';
  let input = usage.prompt_tokens ?? usage.input_tokens ?? usage.promptTokenCount;
  if (typeof input !== 'number') return '服务商未提供';
  if (usage.cache_read_input_tokens != null) input += cached + (usage.cache_creation_input_tokens || 0);
  return `${input ? Math.round(Math.min(1, Math.max(0, cached / input)) * 100) : 0}%`;
}
