export function contextKind(source: string): string {
  const value = source || 'other';
  return /^lore(?:_|:|$)/.test(value) ? 'lore' : value.replace(/_.*/, '') || 'other';
}

export function summarizeBudgetSources(
  sources: Record<string, number> | null | undefined,
  total: number,
  labelFor: (source: string) => string,
) {
  const totals = new Map<string, number>();
  for (const [source, tokens] of Object.entries(sources || {})) {
    const label = labelFor(source);
    totals.set(label, (totals.get(label) || 0) + Number(tokens));
  }
  return Array.from(totals, ([label, tokens]) => ({
    label,
    tokens,
    percent: Math.round((tokens / Math.max(1, total)) * 10000) / 100,
  }));
}

export function groupContext(items: any[], labelFor: (source: string) => string) {
  const groups: Array<{
    id: string;
    source: string;
    kind: string;
    label: string;
    anchor: string;
    items: any[];
    tokens: number;
    count: number;
  }> = [];
  for (const item of items) {
    const source = String(item.source || 'other'),
      kind = item.lore_ids?.length ? 'lore' : contextKind(source);
    const anchor = String(item.anchor_id || '');
    const key =
      kind === 'lore'
        ? item.lore_region === 'constant'
          ? 'lore_constant'
          : anchor
            ? 'lore_conversation'
            : item.insertion_depth != null
              ? 'lore_depth'
              : 'lore'
        : source;
    let group = groups.at(-1);
    // Group only adjacent blocks. Combining a category across the request would
    // move rolling lore above examples and collapse separated history segments.
    if (!group || group.source !== key || group.anchor !== anchor) {
      group = {
        id: `${key}:${groups.length}`,
        source: key,
        kind,
        label: labelFor(key),
        anchor,
        items: [],
        tokens: 0,
        count: 0,
      };
      groups.push(group);
    }
    group.items.push(item);
    group.tokens += Number(item.estimated_tokens || 0);
  }
  for (const group of groups) {
    group.count =
      group.kind === 'lore'
        ? new Set(group.items.flatMap((item) => (item.lore_ids?.length ? item.lore_ids : [item.source]))).size
        : group.items.length;
  }
  return groups;
}
