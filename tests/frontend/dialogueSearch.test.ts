import { describe, expect, it } from 'vitest';
import { textMatches } from '../../web/lib/dialogueSearch';

describe('dialogue full-history search', () => {
  it('treats regex punctuation as literal text', () => {
    expect(textMatches('a+b [x] a+b', 'a+b')).toEqual([
      { start: 0, end: 3 },
      { start: 8, end: 11 },
    ]);
    expect(textMatches('[x] (x) .*', '[x]')).toEqual([{ start: 0, end: 3 }]);
    expect(textMatches('[x] (x) .*', '.*')).toEqual([{ start: 8, end: 10 }]);
  });
  it('preserves original offsets with Unicode and case-insensitive matching', () => {
    expect(textMatches('İ Hello HELLO', 'hello')).toEqual([
      { start: 2, end: 7 },
      { start: 8, end: 13 },
    ]);
    expect(textMatches('你好🌙，你好🌙', '你好🌙')).toEqual([
      { start: 0, end: 4 },
      { start: 5, end: 9 },
    ]);
  });
  it('does not produce empty matches or overlapping duplicate results', () => {
    expect(textMatches('正文', '  ')).toEqual([]);
    expect(textMatches('aaaaa', 'aa')).toEqual([
      { start: 0, end: 2 },
      { start: 2, end: 4 },
    ]);
    expect(textMatches('正文', '缺失')).toEqual([]);
  });
});
