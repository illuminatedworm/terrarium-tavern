export type LoreRecord = {
  id: string;
  anchor_id: string;
  title: string;
  manually_removed?: boolean;
};

export type DialogueLoreRecord = Pick<LoreRecord, 'id' | 'anchor_id' | 'title'> & { source: string };
export type LoreInjectionPayload = {
  source: 'runtime_lore_injection';
  ref: string | null;
  title?: string;
} & ({ preview: string } | { text: string });

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

export function dialogueLoreRecords(
  messages: any[],
  pending?: { turnId: string; inputId?: string; records: LoreRecord[]; regenerating?: boolean },
): Map<string, DialogueLoreRecord[]> {
  const anchors = new Set(messages.map((message) => message.id));
  if (pending?.inputId) anchors.add(pending.inputId);
  const seen = new Set<string>(),
    records = new Map<string, DialogueLoreRecord[]>();
  function collect(entries: LoreRecord[], source: string) {
    for (const { id, anchor_id, title, manually_removed } of entries) {
      if (manually_removed || !anchors.has(anchor_id)) continue;
      const key = JSON.stringify([anchor_id, id]);
      if (seen.has(key)) continue;
      seen.add(key);
      const list = records.get(anchor_id) || [];
      list.push({ id, anchor_id, title, source });
      records.set(anchor_id, list);
    }
  }
  const draftSource = pending ? `turns/${encodeURIComponent(pending.turnId)}` : '';
  if (pending?.regenerating) collect(pending.records, draftSource);
  for (const message of messages) {
    if (message.lore_admissions?.length)
      collect(message.lore_admissions, `messages/${encodeURIComponent(message.id)}`);
  }
  // Old stories may have only the first snapshot that retained an injection.
  for (const message of messages) {
    if (message.snapshot?.lore_injections?.length)
      collect(message.snapshot.lore_injections, `messages/${encodeURIComponent(message.id)}`);
  }
  if (pending && !pending.regenerating) collect(pending.records, draftSource);
  return records;
}
