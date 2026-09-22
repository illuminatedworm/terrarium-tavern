import { describe, expect, it } from 'vitest';
import { dialogueLoreRecords, visibleLoreData } from '../../web/lib/loreRecords';

describe('dialogue lore records', () => {
  it('hides internal records throughout review and event payloads without changing state', () => {
    const marker = { id: 'e', anchor_id: 'g', manually_removed: true };
    const payload = {
      before: { lore_injections: [marker] },
      messages: [{ snapshot: { lore_injections: [marker, { id: 'other' }] } }],
    };
    expect(visibleLoreData(payload)).toEqual({
      before: { lore_injections: [] },
      messages: [{ snapshot: { lore_injections: [{ id: 'other' }] } }],
    });
    expect(payload.before.lore_injections).toEqual([marker]);
  });
  it('never exposes internal removal markers as visible records', () => {
    expect(
      dialogueLoreRecords([
        { id: 'g', snapshot: { lore_injections: [{ id: 'e', anchor_id: 'g', manually_removed: true }] } },
      ]).size,
    ).toBe(0);
  });
  it('keeps historical injections, deduplicates snapshots and allows later reinjection', () => {
    const first = { id: 'e', anchor_id: 'g', title: '设定', content: '原始内容' };
    const later = { ...first, anchor_id: 'u2', content: '后来内容' };
    const records = dialogueLoreRecords([
      { id: 'g', snapshot: {} },
      { id: 'u1', snapshot: { lore_injections: [first] } },
      { id: 'a1', snapshot: { lore_injections: [first] } },
      { id: 'u2', snapshot: { lore_injections: [later] } },
      { id: 'a2', snapshot: { lore_injections: [] } },
    ]);
    expect([...records.keys()]).toEqual(['g', 'u2']);
    expect(records.get('g')).toEqual([first]);
    expect(records.get('u2')).toEqual([later]);
  });
  it('ignores records anchored outside the current branch', () => {
    expect(
      dialogueLoreRecords([
        { id: 'g', snapshot: { lore_injections: [{ id: 'e', anchor_id: 'other-branch' }] } },
      ]).size,
    ).toBe(0);
  });
});
