export type LoreRecord = {
  id: string;
  anchor_id: string;
  title: string;
  content: string;
  manually_removed?: boolean;
};

// Also hide internal markers in review/diagnostic trees, not just the transcript.
export function visibleLoreData<T>(value: T): T {
  if (!value || typeof value !== 'object') return value;
  let result: any = value;
  for (const [key, item] of Object.entries(value)) {
    let selected = item;
    if (
      (key === 'lore_injections' || key === 'next_lore_injections') &&
      Array.isArray(item) &&
      item.some((record) => record?.manually_removed)
    )
      selected = item.filter((record) => !record?.manually_removed);
    const next = visibleLoreData(selected);
    if (next !== item) {
      if (result === value) result = Array.isArray(value) ? [...value] : { ...value };
      result[key] = next;
    }
  }
  return result;
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
