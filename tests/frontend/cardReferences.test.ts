import { describe, expect, it } from 'vitest';
import { cardMentionQuery, cardReferenceOptions } from '../../web/lib/cardReferences';
import type { EditorCard } from '../../web/lib/cardAuthor';

describe('card reference selection at the caret', () => {
  it('recognizes Chinese names without consuming surrounding prose', () => {
    const text = '保留 @C1 的人设，参考@雪山 后文不要改';
    const caret = text.indexOf(' 后文');
    const query = cardMentionQuery(text, caret)!;
    expect(query.query).toBe('雪山');
    expect(text.slice(0, query.start) + text.slice(query.end)).toBe('保留 @C1 的人设，参考 后文不要改');
    expect(cardMentionQuery('@', 1)).toEqual({ start: 0, end: 1, query: '' });
  });
  it('ignores email addresses, escaped double at signs, and selections', () => {
    expect(cardMentionQuery('mail@example.com', 16)).toBeNull();
    expect(cardMentionQuery('@@', 2)).toBeNull();
    expect(cardMentionQuery('@卡片', 0, 3)).toBeNull();
    expect(cardMentionQuery('普通文字', 4)).toBeNull();
  });
  it('preserves text after the insertion caret', () => {
    const text = '参考@后文';
    const query = cardMentionQuery(text, 3)!;
    expect(query.end).toBe(3);
    expect(text.slice(0, query.start) + text.slice(query.end)).toBe('参考后文');
  });
});

describe('card reference candidates', () => {
  const attached: EditorCard[] = [
    { ref: 'C1', card: 'source-a', document: { name: '修改后的雪山' }, source: { name: '原始雪山' } },
    { ref: 'C2', card: null, document: { spec: 'chara_card_v2', data: { name: '新草稿' } }, source: {} },
  ].map((card) => ({
    ...card,
    kind: 'draft',
    base_document: {},
    base_revision: null,
    target_mode: null,
    brief: '',
  }));
  const library = [
    { id: 'source-a', name: '原始雪山', revision: 2 },
    { id: 'source-b', name: '河流', revision: 0 },
  ];
  it('uses existing drafts once and includes unsaved cards', () => {
    const options = cardReferenceOptions(attached, library, '');
    expect(options.map((option) => option.name)).toEqual(['修改后的雪山', '新草稿', '河流']);
    expect(options[0].attached).toBe(attached[0]);
    expect(options[0].source).toBeUndefined();
  });
  it('filters by names and types without displaying internal handles', () => {
    expect(cardReferenceOptions(attached, library, '原始')[0].attached?.ref).toBe('C1');
    expect(cardReferenceOptions(attached, library, 'c2')).toEqual([]);
    expect(
      cardReferenceOptions(attached, library, '')
        .map((item) => item.detail)
        .join(' '),
    ).not.toMatch(/C\d/);
    expect(cardReferenceOptions(attached, library, '不存在')).toEqual([]);
  });
  it('searches and deduplicates user personas independently of same-name cards', () => {
    const persona = { id: 'source-a', name: '原始雪山', description: '玩家设定', version: 3 };
    const options = cardReferenceOptions(attached, library, '用户角色', [persona], { P3: persona });
    expect(options).toHaveLength(1);
    expect(options[0].personaRef).toBe('P3');
    expect(options[0].detail).toBe('用户角色 · 已引用');
    expect(cardReferenceOptions(attached, library, '雪山', [persona])).toHaveLength(2);
  });
  it('offers library worldbooks once, separately from cards with the same name', () => {
    const books = [
      { id: 'book-a', name: '雪山', revision: 1 },
      { id: 'book-b', name: '河流', revision: 0 },
    ];
    const draft = {
      ...attached[0],
      ref: 'C3',
      card: null,
      worldbook: 'book-a',
      document: { name: '雪山', character_book: { entries: [] } },
    };
    const options = cardReferenceOptions([...attached, draft], library, '', [], {}, books);
    expect(options.filter((option) => option.detail.startsWith('世界书')).map((o) => o.detail)).toEqual([
      '世界书 · 已引用',
      '世界书',
    ]);
    expect(options.find((option) => option.worldbookSource)?.worldbookSource).toBe(books[1]);
  });
});
