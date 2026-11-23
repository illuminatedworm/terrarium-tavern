import { describe, expect, it } from 'vitest';
import { dialogueRanges, openingProse, openingSnippet } from '../../web/lib/storyText';

const dialogue = (text: string) => dialogueRanges(text).map(([a, b]) => text.slice(a, b));
describe('story dialogue detection', () => {
  it('hides opening initialization markup while preserving narrative', () => {
    expect(
      openingProse(
        '断界\r\n<UpdateVariable>\r\n<initvar>hp: 100</initvar>\r\n</UpdateVariable><StatusPlaceHolderImpl/>',
      ),
    ).toBe('断界\r\n');
  });
  it('takes the opening snippet from the prose after multiline HTML comments', () => {
    const note = `<!--\r\n${'时间节点 > 作者备注\r\n'.repeat(20)}-->`;
    expect(openingSnippet(`${note}\n阴云<!-- 天气 -->笼罩上空。`)).toBe('阴云笼罩上空。');
  });
  it('recognizes Chinese and English quotation pairs', () => {
    expect(dialogue('他说：“你好。” 「再见」 『晚安』 "Hello." ‘Welcome’ \'Come in\'')).toEqual([
      '“你好。”',
      '「再见」',
      '『晚安』',
      '"Hello."',
      '‘Welcome’',
      "'Come in'",
    ]);
  });
  it('does not highlight apostrophes or incomplete streaming quotes', () => {
    expect(dialogue("It's John's book. 'Hello' and “未完")).toEqual(["'Hello'"]);
  });
  it('keeps quote detection within a paragraph or code boundary', () => {
    expect(dialogue('“前半\n后半”')).toEqual([]);
  });
  it('allows contractions inside quoted dialogue', () => {
    expect(dialogue("'Don't leave John's book here.'")).toEqual(["'Don't leave John's book here.'"]);
  });
});
