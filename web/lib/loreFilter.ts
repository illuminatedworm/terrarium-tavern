export const audiences: Record<string, string> = {
  writer: 'Writer 叙事',
  tracker_mvu: 'Tracker 变量规则',
  runtime_init: '运行时初始化',
};
export const emptyLoreFilter = () => ({ query: '', enabled: 'all', trigger: 'all', audience: 'all' });
export type LoreFilter = ReturnType<typeof emptyLoreFilter>;
export function entryAudience(entry: any): string {
  return entry.extensions?.terrarium?.audience || 'writer';
}
export function filterLore(entries: any[], filter: LoreFilter) {
  const terms = filter.query.toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return entries
    .map((entry, index) => ({ entry, index }))
    .filter(({ entry, index }) => {
      const text = [
        entry.name,
        entry.comment,
        entry.id ?? entry.uid ?? index,
        entry.keys,
        entry.key,
        entry.secondary_keys,
        entry.keysecondary,
        entry.content,
      ]
        .flat()
        .join(' ')
        .toLocaleLowerCase();
      const enabled = entry.enabled ?? !entry.disable;
      return (
        terms.every((term) => text.includes(term)) &&
        (filter.enabled === 'all' || enabled === (filter.enabled === 'enabled')) &&
        (filter.trigger === 'all' || !!entry.constant === (filter.trigger === 'constant')) &&
        (filter.audience === 'all' || entryAudience(entry) === filter.audience)
      );
    });
}
