type LoreEntry = Record<string, any>;
export interface LoreBook {
  entries?: LoreEntry[] | Record<string, LoreEntry>;
  [key: string]: any;
}

const isObject = (value: unknown): value is Record<string, any> =>
  value !== null && typeof value === 'object' && !Array.isArray(value);

/** Match cards.lore_title: ST's comment first, the optional V2/V3 name as fallback. */
export function loreTitle(entry: LoreEntry | null | undefined): string {
  return String(entry?.comment || entry?.name || '');
}

export function isInitVar(entry: LoreEntry | null | undefined): boolean {
  return /\[initvar\]/i.test(loreTitle(entry));
}

const loreFlagAliases = {
  matchWholeWords: 'match_whole_words',
  excludeRecursion: 'exclude_recursion',
  preventRecursion: 'prevent_recursion',
  caseSensitive: 'case_sensitive',
} as const;
type LoreFlag = keyof typeof loreFlagAliases;

function loreFlagSources(entry: LoreEntry, field: LoreFlag) {
  // Match cards.lore_entries: camelCase first, extensions first within each spelling.
  return [field, loreFlagAliases[field]].flatMap((key) => [
    ...(entry.extensions && Object.hasOwn(entry.extensions, key)
      ? [{ owner: entry.extensions as LoreEntry, key, path: `extensions.${key}` }]
      : []),
    ...(Object.hasOwn(entry, key) ? [{ owner: entry, key, path: key }] : []),
  ]);
}

export function loreFlagValue(entry: LoreEntry, field: LoreFlag): boolean {
  const source = loreFlagSources(entry, field)[0];
  return source ? Boolean(source.owner[source.key]) : false;
}

export function loreFlagHelp(entry: LoreEntry, field: LoreFlag): string {
  const [source, ...others] = loreFlagSources(entry, field);
  if (!source) return `未设置，默认关闭；修改后写入 extensions.${loreFlagAliases[field]}。`;
  const current = `当前生效：${source.path} = ${JSON.stringify(source.owner[source.key])}。`;
  if (!others.length) return `${current}修改开关会写回此字段。`;
  const values = others.map((item) => `${item.path} = ${JSON.stringify(item.owner[item.key])}`);
  const conflict = others.some((item) => item.owner[item.key] !== source.owner[source.key]);
  return `${current}${conflict ? '同义字段冲突' : '其他同义字段'}：${values.join('；')}。修改开关会同步所有已有同义字段。`;
}

export function setLoreFlag(entry: LoreEntry, field: LoreFlag, value: boolean): void {
  const sources = loreFlagSources(entry, field);
  if (!sources.length) (entry.extensions ??= {})[loreFlagAliases[field]] = value;
  else for (const source of sources) source.owner[source.key] = value;
}

function entryId(entry: LoreEntry, key: string, keyed: boolean) {
  if (!isObject(entry) || (entry.extensions !== undefined && !isObject(entry.extensions)))
    throw Error('世界书条目及其扩展必须为对象');
  const view = { ...entry, ...(keyed ? { uid: key } : {}), ...entry.extensions };
  const id = Object.hasOwn(view, 'id') ? view.id : Object.hasOwn(view, 'uid') ? view.uid : key;
  // Match the runtime's string identities, including nullable legacy IDs.
  return id === null ? 'None' : typeof id === 'boolean' ? (id ? 'True' : 'False') : String(id);
}

export function appendLoreBook(current: LoreBook | null | undefined, imported: unknown): LoreBook {
  if (!isObject(imported) || (!Array.isArray(imported.entries) && !isObject(imported.entries)))
    throw Error('世界书 entries 无效');
  const book: LoreBook = JSON.parse(
    JSON.stringify(current ?? { ...imported, entries: Array.isArray(imported.entries) ? [] : {} }),
  );
  const entries = (book.entries ??= []);
  if (!Array.isArray(entries) && !isObject(entries)) throw Error('世界书 entries 无效');
  const used = new Set(
    Object.entries(entries).map(([key, entry]) => entryId(entry, key, !Array.isArray(entries))),
  );
  for (const [key, raw] of Object.entries(imported.entries)) {
    let id = entryId(raw, key, !Array.isArray(imported.entries));
    const entry: LoreEntry = JSON.parse(JSON.stringify(raw));
    if (Array.isArray(entries) && !Array.isArray(imported.entries)) entry.uid = key;
    const collision = used.has(id);
    if (collision) {
      do id = crypto.randomUUID();
      while (used.has(id));
    }
    // Imported index/key identities must stay stable at their new location.
    if (entry.id === undefined || collision) entry.id = id;
    if (collision && entry.extensions?.id !== undefined) entry.extensions.id = id;
    used.add(id);
    if (Array.isArray(entries)) entries.push(entry);
    else {
      let target = Array.isArray(imported.entries) ? id : key;
      while (Object.hasOwn(entries, target)) target = crypto.randomUUID();
      Object.defineProperty(entries, target, {
        value: entry,
        enumerable: true,
        writable: true,
        configurable: true,
      });
    }
  }
  return book;
}
