export type TextMatch = { start: number; end: number };

/** Literal, case-insensitive matching with offsets in the original text. */
export function textMatches(text: string, query: string): TextMatch[] {
  if (!query.trim()) return [];
  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return Array.from(text.matchAll(new RegExp(escaped, 'giu')), (match) => ({
    start: match.index!,
    end: match.index! + match[0].length,
  }));
}
