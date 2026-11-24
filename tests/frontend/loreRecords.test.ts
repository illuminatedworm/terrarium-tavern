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
  it('locates each preview at its original snapshot or pending turn across reinjections', () => {
    const first = { id: 'e', anchor_id: 'g', title: '设定', content: '原始内容' };
    const later = { ...first, anchor_id: 'u2', content: '后来内容' };
    const messages = [
      { id: 'g', snapshot: {} },
      { id: 'u1', snapshot: { lore_injections: [first] } },
      { id: 'a1', lore_admissions: [first], snapshot: { lore_injections: [] } },
      { id: 'u2', snapshot: { lore_injections: [later] } },
      { id: 'a2', snapshot: { lore_injections: [] } },
    ];
    const records = dialogueLoreRecords(messages);
    expect([...records.keys()]).toEqual(['g', 'u2']);
    expect(records.get('g')).toEqual([{ id: 'e', anchor_id: 'g', title: '设定', source: 'messages/a1' }]);
    expect(records.get('u2')).toEqual([{ id: 'e', anchor_id: 'u2', title: '设定', source: 'messages/u2' }]);
    const draft = {
      turnId: 'retry',
      inputId: 'new-input',
      records: [later, { ...later, anchor_id: 'new-input' }],
    };
    const pending = dialogueLoreRecords(messages, draft);
    expect(pending.get('u2')?.[0].source).toBe('messages/u2');
    expect(pending.get('new-input')?.[0].source).toBe('turns/retry');
    expect(dialogueLoreRecords(messages, { ...draft, regenerating: true }).get('u2')?.[0].source).toBe(
      'turns/retry',
    );
  });
  it('ignores records anchored outside the current branch', () => {
    expect(
      dialogueLoreRecords([
        { id: 'g', snapshot: { lore_injections: [{ id: 'e', anchor_id: 'other-branch' }] } },
      ]).size,
    ).toBe(0);
  });
});
