import { describe, expect, it } from 'vitest';
import { groupContext, summarizeBudgetSources } from '../../web/lib/contextPreview';

describe('context preview budget summary', () => {
  it('combines sources with the same visible category before rounding percentages', () => {
    const sources = {
      'lore:first': 3,
      history: 20,
      'lore:second': 2,
      chat_framing: 1,
    };
    const summary = summarizeBudgetSources(sources, 26, (source) =>
      source.startsWith('lore:') ? '世界书（按设定位置）' : source,
    );
    expect(summary).toEqual([
      { label: '世界书（按设定位置）', tokens: 5, percent: 19.23 },
      { label: 'history', tokens: 20, percent: 76.92 },
      { label: 'chat_framing', tokens: 1, percent: 3.85 },
    ]);
  });
  it('keeps small nonzero percentages and rounds to two decimal places', () => {
    const summary = summarizeBudgetSources({ small: 1, rest: 799 }, 800, (source) => source);
    expect(summary[0].percent).toBe(0.13);
    expect(summary[0].percent.toFixed(2)).toBe('0.13');
  });
});

describe('context preview categories', () => {
  it('shows the entire constant region as one top lore group', () => {
    const items = Array.from({ length: 8 }, (_, id) => ({
      source: `lore:${id}`,
      lore_ids: [String(id)],
      lore_region: 'constant',
    }));
    const groups = groupContext([...items, { source: 'history' }], (source) => source);
    expect(groups.map((group) => group.source)).toEqual(['lore_constant', 'history']);
    expect(groups[0].count).toBe(8);
    expect(groups[0].items).toEqual(items);
  });
  it('distinguishes depth insertions without duplicating or moving lore', () => {
    const items = [
      { source: 'lore_before', lore_ids: ['rules'] },
      { source: 'history' },
      { source: 'lore:update', lore_ids: ['update'], insertion_depth: 2 },
      { source: 'history' },
      { source: 'lore:format', lore_ids: ['format'], insertion_depth: 1 },
    ];
    const groups = groupContext(items, (source) => source);
    expect(groups.map((group) => group.source)).toEqual([
      'lore',
      'history',
      'lore_depth',
      'history',
      'lore_depth',
    ]);
    expect(groups.flatMap((group) => group.items)).toEqual(items);
    expect(
      groups.filter((group) => group.kind === 'lore').reduce((total, group) => total + group.count, 0),
    ).toBe(3);
  });
  it('preserves order when lore blocks are separated by another source', () => {
    const groups = groupContext(
      [
        { source: 'lore:1', estimated_tokens: 10 },
        { source: 'persona', estimated_tokens: 5 },
        { source: 'lore:2', estimated_tokens: 20 },
        { source: 'lore_before', estimated_tokens: 30 },
      ],
      (source) => source,
    );
    const lore = groups.filter((group) => group.kind === 'lore');
    expect(groups.map((group) => group.source)).toEqual(['lore', 'persona', 'lore']);
    expect(lore).toHaveLength(2);
    expect(lore[0].items).toHaveLength(1);
    expect(lore[1].items).toHaveLength(2);
    expect(lore.reduce((total, group) => total + group.tokens, 0)).toBe(60);
  });
  it('counts distinct entries rather than aggregated message blocks', () => {
    const groups = groupContext(
      [
        { source: 'lore_before', lore_ids: ['1', '2'] },
        { source: 'lore:2', lore_ids: ['2'] },
        { source: 'author_note', lore_ids: ['3'] },
      ],
      (source) => source,
    );
    expect(groups).toHaveLength(1);
    expect(groups[0].count).toBe(3);
  });
  it.each([true, false])('places rolling lore immediately before first history, examples=%s', (examples) => {
    const items = [
      { source: 'lore_before', lore_ids: ['constant'], position: 0 },
      { source: 'character', position: 1 },
      ...(examples ? [{ source: 'examples', position: 2 }] : []),
      { source: 'lore:triggered', lore_ids: ['triggered'], anchor_id: 'g', position: 3 },
      { source: 'history', message_id: 'g', position: 4 },
      { source: 'history', message_id: 'u1', position: 5 },
      { source: 'lore:later', lore_ids: ['later'], anchor_id: 'u2', position: 6 },
      { source: 'history', message_id: 'u2', position: 7 },
    ];
    const groups = groupContext(items, (source) => source);
    expect(groups.flatMap((group) => group.items)).toEqual(items);
    const rolling = groups.findIndex((group) => group.source === 'lore_conversation');
    expect(groups[rolling - 1].source).toBe(examples ? 'examples' : 'character');
    expect(groups[rolling + 1].source).toBe('history');
    expect(groups.filter((group) => group.source === 'history')).toHaveLength(2);
    expect(new Set(groups.map((group) => group.id)).size).toBe(groups.length);
  });
});
