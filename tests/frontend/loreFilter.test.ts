import { describe, expect, it } from 'vitest';
import { emptyLoreFilter, filterLore } from '../../web/lib/loreFilter';
describe('worldbook filtering', () => {
  it('combines words and filters while preserving source indexes and references', () => {
    const entries = [
      { id: 'a', comment: '其他' },
      {
        id: 'b',
        comment: 'HP 规则',
        content: '战斗 人物',
        enabled: true,
        constant: true,
        extensions: { terrarium: { audience: 'tracker_mvu' } },
      },
      { id: 'c', comment: 'HP 规则', content: '战斗', enabled: false },
    ];
    const result = filterLore(entries, {
      query: 'hp 人物',
      enabled: 'enabled',
      trigger: 'constant',
      audience: 'tracker_mvu',
    });
    expect(result.map((x) => x.index)).toEqual([1]);
    result[0].entry.content = '修改目标';
    expect(entries[1].content).toBe('修改目标');
    expect(entries[2].content).toBe('战斗');
    expect(filterLore(entries, emptyLoreFilter())).toHaveLength(3);
  });

  it('reflects batch injection order without reordering source entries or losing edit targets', () => {
    const entries = [
      { id: '10', insertion_order: 20 },
      { id: '2', order: 20 },
      { id: 'first', insertion_order: 90, extensions: { insertion_order: 10 } },
      { id: 'later', constant: true, insertion_order: 1, extensions: { position: 1 } },
      { id: 'before', constant: true, insertion_order: 90, position: 'before_char' },
      { id: 'shallow', constant: true, extensions: { position: 4, depth: 1 } },
      { id: 'deep', constant: true, extensions: { position: 4, depth: 3 } },
      { id: 'a', insertion_order: 20 },
      { id: 'after', constant: true, insertion_order: 2, position: 'after_char' },
      { id: 'default' },
      { insertion_order: 20 },
    ];
    const before = JSON.stringify(entries);
    const result = filterLore(entries, emptyLoreFilter());
    expect(result.map((item) => item.index)).toEqual([4, 3, 8, 6, 5, 2, 1, 0, 10, 7, 9]);
    expect(JSON.stringify(entries)).toBe(before);
    expect(result[0].entry).toBe(entries[4]);
    const conditional = filterLore(entries, { ...emptyLoreFilter(), trigger: 'conditional' });
    expect(conditional.map((item) => item.index)).toEqual([2, 1, 0, 10, 7, 9]);
  });
});
