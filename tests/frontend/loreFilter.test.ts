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
});
