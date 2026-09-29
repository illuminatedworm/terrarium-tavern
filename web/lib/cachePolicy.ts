export interface CacheSettings {
  mode: 'cloud' | 'runtime';
  preset: 'auto' | 'custom';
  field: string;
  value: unknown;
  request: Record<string, unknown>;
}

export function cachePreset(
  model: string,
  api: string,
  cache: CacheSettings,
): {
  kind: string;
  field?: string;
  value?: unknown;
  request?: Record<string, unknown>;
} {
  if (cache.preset === 'custom') return { kind: 'block', field: cache.field, value: cache.value };
  const name = model.toLowerCase();
  if (api === 'gemini' || (api !== 'anthropic' && name.includes('gemini'))) return { kind: 'gemini' };
  if (api === 'anthropic' || name.includes('claude'))
    return { kind: 'block', field: 'cache_control', value: { type: 'ephemeral' } };
  if (name.includes('gpt') || /^(o1|o3|o4)/.test(name))
    return {
      kind: 'block',
      field: 'prompt_cache_breakpoint',
      value: { mode: 'explicit' },
      request: { prompt_cache_options: { mode: 'explicit' } },
    };
  return { kind: 'unknown' };
}

export function modelEndpoint(base: string, api: string, model = '{model}') {
  try {
    const url = new URL(base);
    let path = url.pathname.replace(/\/$/, '');
    if (api === 'gemini') {
      path = path.replace(/\/v1$/, '').replace(/\/openai$/, '');
      if (!/\/v1(beta|alpha)$/.test(path)) path += '/v1beta';
      path += '/models/' + encodeURIComponent(model.replace(/^models\//, '')) + ':generateContent';
    } else {
      const suffix =
        api === 'responses' ? '/responses' : api === 'anthropic' ? '/messages' : '/chat/completions';
      if (path.endsWith(suffix)) path = path.slice(0, -suffix.length);
      if (!path) path = '/v1';
      else if (api === 'anthropic' && !path.endsWith('/v1')) path += '/v1';
      path += suffix;
    }
    return url.origin + path;
  } catch {
    return '请输入有效的接口地址';
  }
}
