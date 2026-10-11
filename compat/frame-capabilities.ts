/** Explicit opt-in only. Legacy scripts may register callbacks indirectly or later. */
export function frameCapabilities(html: string) {
  const tags =
    html.match(
      /<!--[\s\S]*?-->|<(script|style|textarea|title)\b[^>]*>[\s\S]*?<\/\1\s*>|<meta\b(?:[^>"']|"[^"]*"|'[^']*')*>/gi,
    ) || [];
  const values = new Set<string>();
  for (const tag of tags) {
    if (!/^<meta\b/i.test(tag)) continue;
    const attrs = Object.fromEntries(
      [...tag.matchAll(/([\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/g)].map((match) => [
        match[1].toLowerCase(),
        match[2] ?? match[3],
      ]),
    );
    if (attrs.name?.toLowerCase() === 'tavern-capabilities')
      for (const value of (attrs.content || '').split(/\s+/)) values.add(value);
  }
  return {
    displayOnly: values.has('display-only'),
    liveState: values.has('live-state'),
    suspend: values.has('suspend'),
    sharedAssets: values.has('shared-assets'),
  };
}
