export type LoreRecord = {
  id: string;
  anchor_id: string;
  title: string;
  content: string;
  manually_removed?: boolean;
};

// Also hide internal markers in review/diagnostic trees, not just the transcript.
export function visibleLoreData<T>(value: T): T {
  if (Array.isArray(value)) return value.map(visibleLoreData) as T;
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [
      key,
      visibleLoreData(
        (key === 'lore_injections' || key === 'next_lore_injections') && Array.isArray(item)
          ? item.filter((record) => !record?.manually_removed)
          : item,
      ),
    ]),
  ) as T;
}

export function dialogueLoreRecords(messages: any[]): Map<string, LoreRecord[]> {
  const anchors = new Set(messages.map((message) => message.id));
  const seen = new Set<string>(),
    records = new Map<string, LoreRecord[]>();
  // Older snapshots preserve injections that have since left the model window.
  for (const message of messages) {
    for (const record of message.snapshot?.lore_injections || []) {
      if (record.manually_removed || !anchors.has(record.anchor_id)) continue;
      const key = JSON.stringify([record.anchor_id, record.id]);
      if (seen.has(key)) continue;
      seen.add(key);
      const list = records.get(record.anchor_id) || [];
      list.push(record);
      records.set(record.anchor_id, list);
    }
  }
  return records;
}
