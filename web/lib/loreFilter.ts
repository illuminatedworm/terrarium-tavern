import { loreField } from './loreBook';

export const audiences: Record<string, string> = {
  writer: 'Writer 叙事',
  tracker_mvu: 'Tracker 变量规则',
};
export const emptyLoreFilter = () => ({ query: '', enabled: 'all', trigger: 'all', audience: 'all' });
export type LoreFilter = ReturnType<typeof emptyLoreFilter>;
export function entryAudience(entry: any): string {
  return entry.extensions?.terrarium?.audience || 'writer';
}
function loreOrder({ entry, index }: { entry: any; index: number }) {
  const field = (name: string) => loreField(entry, name);
  const constant = field('constant');
  const position = constant
    ? field('position') === 'before_char'
      ? 0
      : field('position') === 'after_char'
        ? 1
        : Number(field('position') ?? 0)
    : 0;
  const id = String(field('id') ?? field('uid') ?? index);
  return [
    constant ? 0 : 1,
    position,
    position === 4 ? -Number(field('depth') || 0) : 0,
    Number(field('insertion_order') ?? 100),
    /^\d+$/.test(id) ? 0 : 1,
    /^\d+$/.test(id) ? BigInt(id) : id,
  ] as const;
}
export function filterLore(entries: any[], filter: LoreFilter) {
  const terms = filter.query.toLocaleLowerCase().split(/\s+/).filter(Boolean);
  // Sort only the display projection, preserving source indexes for edits/deletes.
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
    })
    .sort((a, b) => {
      const left = loreOrder(a),
        right = loreOrder(b);
      for (let index = 0; index < left.length; index++) {
        if (left[index] < right[index]) return -1;
        if (left[index] > right[index]) return 1;
      }
      return 0;
    });
}
