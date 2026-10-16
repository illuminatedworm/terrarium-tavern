import { geminiControls } from './geminiCapabilities';

export interface CacheSettings {
  mode: 'cloud' | 'runtime';
  preset: 'auto' | 'custom';
  field: string;
  value: unknown;
  request: Record<string, unknown>;
  retention?: string;
}

export function cacheRetention(model: string, api: string, cache: CacheSettings) {
  const name = model.toLowerCase();
  const apiLabel =
    (
      {
        anthropic: 'Anthropic Messages',
        gemini: 'Google GenAI · Gemini',
        chat_completions: 'Chat Completions',
        responses: 'Responses',
      } as Record<string, string>
    )[api] || api;
  const family = name.includes('gemini')
    ? 'Gemini'
    : name.includes('claude')
      ? 'Claude'
      : name.includes('gpt') || /^(o1|o3|o4)(-|$)/.test(name)
        ? 'OpenAI'
        : '';
  const nativeFamily = api === 'anthropic' ? 'Claude' : api === 'gemini' ? 'Gemini' : '';
  const protocolNote =
    nativeFamily && family && family !== nativeFamily
      ? `模型名称属于 ${family}，接入方式仍为 ${apiLabel}。缓存档位按接入协议匹配；切换模型名称不会自动更改接口方式。`
      : '';
  if (!geminiControls(api, model).explicitCache) {
    return {
      model,
      apiLabel,
      protocolNote,
      kind: '',
      value: '',
      enabled: false,
      explicit: false,
      note: '当前 Gemini 模型仅支持隐式缓存，缓存前缀和时效由服务端管理；已保存的本地缓存参数不发送。',
      options: [] as Array<{ value: string; label: string }>,
    };
  }
  let kind = '';
  let values: string[] = [];
  const customClaude =
    cache.preset === 'custom' &&
    cache.field === 'cache_control' &&
    (cache.value as any)?.type === 'ephemeral';
  if (
    (cache.preset !== 'custom' || customClaude) &&
    (api === 'anthropic' || (api === 'chat_completions' && (name.includes('claude') || customClaude)))
  ) {
    kind = 'anthropic';
    values = ['5m', '1h'];
  } else if (
    api === 'gemini' ||
    (api === 'chat_completions' && cache.preset !== 'custom' && name.includes('gemini'))
  ) {
    kind = 'gemini';
    values = ['5m', '15m', '1h', '24h'];
  } else if (['chat_completions', 'responses'].includes(api)) {
    const match = name.match(/(?:^|\/)gpt-(\d+)(?:[.-](\d+))?(?:-|$)/);
    const major = Number(match?.[1]),
      minor = Number(match?.[2] || 0);
    if (match && (major > 5 || (major === 5 && minor >= 6))) {
      kind = 'openai_ttl';
      values = ['30m'];
    } else if (match && major === 5 && minor === 5) {
      kind = 'openai_retention';
      values = ['24h'];
    } else if (match && ((major === 5 && [0, 1, 2, 4].includes(minor)) || (major === 4 && minor === 1))) {
      kind = 'openai_retention';
      values = ['in_memory', '24h'];
    } else if (name.includes('gpt') || /^(o1|o3|o4)(-|$)/.test(name)) {
      kind = 'openai_retention';
      values = ['in_memory'];
    }
  }
  const labels: Record<string, string> = {
    '5m': '5 分钟',
    '15m': '15 分钟',
    '1h': '1 小时',
    '24h': '24 小时',
    '30m': '30 分钟',
    in_memory: '内存缓存（通常 5–10 分钟）',
  };
  const value = values.includes(cache.retention || '') ? cache.retention! : values[0] || '';
  const enabled =
    !!kind &&
    (cache.mode === 'runtime' || kind.startsWith('openai') || (kind === 'anthropic' && api === 'anthropic'));
  const note = !kind
    ? '当前接入方式没有可选的缓存时效参数。'
    : !enabled
      ? '云端默认缓存的时效由服务商管理；切换本地运行时控制后可设置。'
      : kind === 'anthropic'
        ? cache.mode === 'cloud'
          ? '保存后使用原生自动缓存，由服务端选择终点；1 小时档的写入价格通常更高。'
          : '1 小时缓存的写入价格通常高于 5 分钟；命中会刷新时效。'
        : kind === 'gemini'
          ? '控制显式缓存对象的保存时长；保存越久，存储费用越高。'
          : kind === 'openai_ttl'
            ? '当前模型支持的最低缓存时效为 30 分钟，仅有这一档。'
            : '实际保留时间由服务端决定，24 小时是最长保留上限。';
  const explicit = !(
    cache.preset === 'auto' &&
    kind === 'openai_retention' &&
    (/(?:^|\/)gpt-\d/.test(name) || /^(o1|o3|o4)(-|$)/.test(name))
  );
  return {
    model,
    apiLabel,
    protocolNote,
    kind,
    value,
    enabled,
    note,
    explicit,
    options: values.map((value) => ({ value, label: labels[value] })),
  };
}

function baseCachePreset(
  model: string,
  api: string,
  cache: CacheSettings,
): {
  kind: string;
  field?: string;
  value?: unknown;
  request?: Record<string, unknown>;
} {
  if (!geminiControls(api, model).explicitCache) return { kind: 'implicit' };
  if (api === 'gemini') return { kind: 'gemini' };
  if (cache.preset === 'custom') return { kind: 'block', field: cache.field, value: cache.value };
  const name = model.toLowerCase();
  if (!cacheRetention(model, api, cache).explicit) return { kind: 'implicit' };
  if (api !== 'anthropic' && name.includes('gemini')) return { kind: 'gemini' };
  if (api === 'anthropic' || (api === 'chat_completions' && name.includes('claude')))
    return {
      kind: 'block',
      field: 'cache_control',
      value: {
        type: 'ephemeral',
        ...(cacheRetention(model, api, cache).value === '1h' ? { ttl: '1h' } : {}),
      },
    };
  if (name.includes('gpt') || /^(o1|o3|o4)/.test(name))
    return {
      kind: 'block',
      field: 'prompt_cache_breakpoint',
      value: { mode: 'explicit' },
      request: { prompt_cache_options: { mode: 'explicit' } },
    };
  return { kind: 'unknown' };
}

export function cachePreset(model: string, api: string, cache: CacheSettings) {
  const result = baseCachePreset(model, api, cache);
  const policy = cacheRetention(model, api, cache);
  if (
    policy.kind === 'anthropic' &&
    result.field === 'cache_control' &&
    typeof result.value === 'object' &&
    result.value !== null
  ) {
    const value = { ...result.value } as Record<string, unknown>;
    if (policy.value === '1h') value.ttl = '1h';
    else delete value.ttl;
    result.value = value;
  }
  if (policy.kind.startsWith('openai')) {
    result.request =
      policy.kind === 'openai_ttl'
        ? {
            ...(result.request || {}),
            prompt_cache_options: {
              ...((result.request?.prompt_cache_options as Record<string, unknown>) || {}),
              ttl: policy.value,
            },
          }
        : { ...(result.request || {}), prompt_cache_retention: policy.value };
  }
  return result;
}

export function modelEndpoint(base: string, api: string, model = '{model}', apiPath?: string) {
  try {
    const url = new URL(base);
    let path = url.pathname.replace(/\/$/, '');
    if (apiPath !== undefined) {
      path = '/' + (apiPath.trim().replace(/^\/+|\/+$/g, '') || (api === 'gemini' ? 'v1beta' : 'v1'));
      path +=
        api === 'gemini'
          ? '/models/' + encodeURIComponent(model.replace(/^models\//, '')) + ':generateContent'
          : api === 'responses'
            ? '/responses'
            : api === 'anthropic'
              ? '/messages'
              : '/chat/completions';
    } else if (api === 'gemini') {
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
