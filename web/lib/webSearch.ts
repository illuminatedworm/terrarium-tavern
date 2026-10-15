export function webServiceLabel(name: string) {
  return (
    (
      {
        exa: 'Exa MCP',
        tavily: 'Tavily',
        searxng: 'SearXNG',
        duckduckgo: 'DuckDuckGo',
        local: '本地网页读取',
        openai: 'ChatGPT 云端搜索',
      } as Record<string, string>
    )[name] || name
  );
}
export function webActivityLabel(p: Record<string, any>) {
  const auth =
    p.auth_mode === 'keyless'
      ? '免 key'
      : p.auth_mode === 'api_key'
        ? '自有 key'
        : p.auth_mode === 'oauth'
          ? 'OAuth'
          : '';
  const status =
    (
      { started: '开始', completed: '完成', error: '失败', fallback: '改用本地搜索' } as Record<
        string,
        string
      >
    )[p.status] || p.status;
  return [webServiceLabel(p.service), auth, status, p.reason].filter(Boolean).join(' · ');
}
export interface WebSource {
  url: string;
  title: string;
}
export function webSources(rows: Array<{ web_sources?: WebSource[] }>): WebSource[] {
  const links = new Map<string, WebSource>();
  for (const row of rows)
    for (const source of row.web_sources || []) {
      try {
        const url = new URL(source.url);
        if (['http:', 'https:'].includes(url.protocol) && !url.username && !url.password)
          links.set(source.url, source);
      } catch {
        /* Invalid source URLs are never rendered as links. */
      }
    }
  return [...links.values()];
}
