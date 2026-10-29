export function contextKind(source: string): string {
  const value = source || 'other';
  if (value === 'agent_tools') return 'history';
  return /^lore(?:_|:|$)/.test(value) ? 'lore' : value.replace(/_.*/, '') || 'other';
}

export function contextMessageContent(item: any): string {
  const parts = [];
  if (item.content)
    parts.push(typeof item.content === 'string' ? item.content : JSON.stringify(item.content, null, 2));
  for (const call of item.tool_calls || []) {
    const fn = call.function || {};
    let args = fn.arguments ?? '';
    try {
      args = JSON.stringify(typeof args === 'string' ? JSON.parse(args) : args, null, 2);
    } catch {
      // Preserve malformed arguments exactly so the preview can explain tool errors.
    }
    parts.push(`工具调用：${fn.name || call.name || '未知工具'}${call.id ? ` · ${call.id}` : ''}\n${args}`);
  }
  if (item.role === 'tool' && item.tool_call_id) parts.unshift(`工具返回 · ${item.tool_call_id}`);
  return parts.join('\n\n') || '（无文本内容）';
}

/** Category shares of the preview-only local tokenizer reference. */
export function summarizeBudgetSources(
  sources: Record<string, number> | null | undefined,
  total: number,
  labelFor: (source: string) => string,
) {
  const totals = new Map<string, { kind: string; tokens: number }>();
  for (const [source, tokens] of Object.entries(sources || {})) {
    const label = labelFor(source);
    const entry = totals.get(label) || { kind: contextKind(source), tokens: 0 };
    entry.tokens += Number(tokens);
    totals.set(label, entry);
  }
  return Array.from(totals, ([label, { kind, tokens }]) => ({
    label,
    kind,
    tokens,
    percent: Math.round((tokens / Math.max(1, total)) * 10000) / 100,
  }));
}

/**
 * One-line row preview: the leading wrapper tag (`<lorebook>`,
 * `<story_history ref='H3' role='user'>`) is shown apart from the text.
 */
export function contextExcerpt(item: any): { tag: string; attrs: Record<string, string>; text: string } {
  let text =
    typeof item.content === 'string' ? item.content : item.content ? JSON.stringify(item.content) : '';
  if (!text && item.tool_calls?.length)
    text = item.tool_calls
      .map((call: any) => `${call.function?.name || call.name || '工具'}(${call.function?.arguments ?? ''})`)
      .join(' ');
  const tag = /^\s*<([A-Za-z][\w-]*)((?:\s+[\w-]+='[^']*')*)\s*>/.exec(text);
  const attrs: Record<string, string> = {};
  if (tag) {
    for (const [, key, value] of tag[2].matchAll(/([\w-]+)='([^']*)'/g)) attrs[key] = value;
    text = text.slice(tag[0].length).replace(new RegExp(`</${tag[1]}>\\s*$`), '');
  }
  return { tag: tag?.[1] || '', attrs, text: text.replace(/\s+/g, ' ').trim().slice(0, 240) };
}

export function groupContext(items: any[], labelFor: (source: string) => string) {
  const groups: Array<{
    id: string;
    source: string;
    kind: string;
    label: string;
    anchor: string;
    items: any[];
    /** Sum of the preview-only local reference; 0 when it was not requested. */
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
        : source === 'agent_tools'
          ? 'history'
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
    group.tokens += Number(item.local_tokens || 0);
  }
  for (const group of groups) {
    group.count =
      group.kind === 'lore'
        ? new Set(group.items.flatMap((item) => (item.lore_ids?.length ? item.lore_ids : [item.source]))).size
        : group.items.length;
  }
  return groups;
}
