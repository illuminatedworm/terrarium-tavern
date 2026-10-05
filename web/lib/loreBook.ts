type LoreEntry = Record<string, any>;
export interface LoreBook {
  entries?: LoreEntry[] | Record<string, LoreEntry>;
  [key: string]: any;
}

const isObject = (value: unknown): value is Record<string, any> =>
  value !== null && typeof value === 'object' && !Array.isArray(value);

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
