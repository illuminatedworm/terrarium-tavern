import { describe, expect, it } from 'vitest';
import { appendLoreBook, loreField, loreFieldHelp, setLoreField } from '../../web/lib/loreBook';
import { appendRegexScripts } from '../../web/lib/regexScripts';

describe('additive worldbook imports', () => {
  it.each([
    ['array', 'array'],
    ['array', 'object'],
    ['object', 'array'],
    ['object', 'object'],
  ])('preserves existing %s entries when importing %s entries', (destination, source) => {
    const old = { id: 0, content: '原有设定', extensions: { custom: { keep: true } } };
    const incoming = [
      { id: '0', content: '新设定', extensions: { custom: { imported: true } } },
      { id: 'fresh', content: '另一条新设定' },
    ];
    const current = {
      name: '当前世界书',
      scan_depth: 7,
      extensions: { keep: true },
      entries: destination === 'array' ? [old] : { same: old },
    };
    const imported = {
      name: '导入世界书',
      scan_depth: 1,
      entries: source === 'array' ? incoming : { same: incoming[0], fresh: incoming[1] },
    };
    const originals = JSON.stringify({ current, imported });
    const merged = appendLoreBook(current, imported);
    const rows = Object.values(merged.entries!);

    expect(rows).toHaveLength(3);
    expect(rows[0]).toEqual(old);
    expect(rows[1]).toMatchObject({ content: '新设定', extensions: incoming[0].extensions });
    expect(rows[1].id).not.toBe('0');
    expect(rows[2]).toMatchObject(incoming[1]);
    expect(new Set(rows.map((entry) => String(entry.id))).size).toBe(3);
    expect(merged).toMatchObject({ name: '当前世界书', scan_depth: 7, extensions: { keep: true } });
    expect(Array.isArray(merged.entries)).toBe(destination === 'array');
    if (destination === 'object') expect(merged.entries).toHaveProperty('same', old);
    expect(JSON.stringify({ current, imported })).toBe(originals);
  });

  it('keeps both entries when their object keys collide but their IDs differ', () => {
    const old = { id: 'old', content: '保留原文' };
    const added = { id: 'new', content: '添加原文' };
    const merged = appendLoreBook({ entries: { same: old } }, { entries: { same: added } });
    expect(merged.entries).toHaveProperty('same', old);
    expect(Object.values(merged.entries!)).toEqual([old, added]);
  });

  it('assigns stable identities to implicit entries across repeated imports', () => {
    const imported = { entries: [{ content: '索引条目' }, { uid: 'named', content: 'UID 条目' }] };
    let book = appendLoreBook({ entries: [{ content: '原有索引条目' }] }, imported);
    book = appendLoreBook(book, imported);
    const rows = Object.values(book.entries!);
    const ids = rows.map((entry, index) => String(entry.id ?? entry.uid ?? index));
    expect(rows.map((entry) => entry.content)).toEqual([
      '原有索引条目',
      '索引条目',
      'UID 条目',
      '索引条目',
      'UID 条目',
    ]);
    expect(new Set(ids).size).toBe(5);
    expect(rows[2].id).toBe('named');
  });

  it('retains imported settings for a card without a worldbook and preserves keyed identities', () => {
    const imported = {
      name: '新世界书',
      extensions: { custom: true },
      entries: { named: { content: '正文' } },
    };
    const book = appendLoreBook(null, imported);
    expect(book).toEqual({ ...imported, entries: { named: { id: 'named', content: '正文' } } });
    const array = appendLoreBook({ entries: [] }, imported);
    expect(array.entries).toEqual([{ id: 'named', uid: 'named', content: '正文' }]);
  });

  it('handles IDs supplied by extensions without hiding newly imported entries', () => {
    const entry = { id: 'source', content: '正文', extensions: { id: 'shared' } };
    const book = appendLoreBook(
      { entries: [{ id: 'shared', content: '原文' }] },
      { entries: [entry, entry] },
    );
    const rows = Object.values(book.entries!);
    expect(new Set(rows.map((row) => row.extensions?.id ?? row.id)).size).toBe(3);
  });

  it.each([
    null,
    [],
    {},
    { entries: null },
    { entries: 'invalid' },
    { entries: [null] },
    { entries: { bad: null } },
    { entries: [{ extensions: [] }] },
    { entries: [{ id: null }] },
    { entries: [{ extensions: { uid: true } }] },
  ])('rejects invalid imports without changing the current book: %j', (imported) => {
    const current = { entries: [{ id: 'old', content: '保留原文' }] };
    const original = JSON.stringify(current);
    expect(() => appendLoreBook(current, imported)).toThrow('世界书');
    expect(JSON.stringify(current)).toBe(original);
  });

  it('keeps an existing book unchanged when importing an empty book', () => {
    const current = { name: '保留', entries: { old: { content: '保留原文' } } };
    expect(appendLoreBook(current, { name: '不覆盖', entries: [] })).toEqual(current);
  });
});

describe('worldbook entry fields', () => {
  it('shows and edits the value the runtime uses across extensions, aliases and nulls', () => {
    const entry: Record<string, any> = {
      depth: 2,
      scanDepth: 6,
      role: 'user',
      extensions: { depth: null, role: 'assistant' },
    };
    expect([loreField(entry, 'depth'), loreField(entry, 'scan_depth'), loreField(entry, 'role')]).toEqual([
      2,
      6,
      'assistant',
    ]);
    expect(loreFieldHelp(entry, 'role', '为 system')).toContain('被覆盖的冲突值：role = "user"');
    expect(loreFieldHelp(entry, 'depth', '为 0')).toContain('extensions.depth 为 null，视为未设置');
    setLoreField(entry, 'role', 'system');
    setLoreField(entry, 'depth', 3);
    setLoreField(entry, 'probability', 50);
    expect(entry).toEqual({
      depth: 3,
      scanDepth: 6,
      role: 'system',
      probability: 50,
      extensions: { depth: 3, role: 'system' },
    });
  });
});

describe('additive regex script imports', () => {
  it('appends ST exports after existing scripts and renews only missing or colliding IDs', () => {
    const current = [{ id: 'keep', scriptName: '原有', findRegex: 'a', custom: { kept: true } }];
    const original = JSON.stringify(current);
    const merged = appendRegexScripts(current, [
      { id: 'keep', scriptName: '同 ID', findRegex: 'b' },
      { id: 'fresh', scriptName: '新脚本', findRegex: 'c', placement: [2] },
      { scriptName: '无 ID', findRegex: 'd' },
    ]);
    expect(JSON.stringify(current)).toBe(original);
    expect(merged.map((script) => script.scriptName)).toEqual(['原有', '同 ID', '新脚本', '无 ID']);
    expect(merged[0]).toEqual(current[0]);
    expect(merged[2]).toEqual({ id: 'fresh', scriptName: '新脚本', findRegex: 'c', placement: [2] });
    expect(new Set(merged.map((script) => script.id)).size).toBe(4);
    expect(appendRegexScripts(undefined, { scriptName: '单个', findRegex: 'e' })).toHaveLength(1);
    expect(() => appendRegexScripts(current, [{ scriptName: '缺少表达式' }])).toThrow('findRegex');
  });
});
