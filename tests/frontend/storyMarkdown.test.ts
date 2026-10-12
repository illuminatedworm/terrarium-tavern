import { describe, expect, it } from 'vitest';
import { marked } from 'marked';
import { parseStoryMarkdown } from '../../web/lib/storyMarkdown';

describe('story Markdown quoted Chinese bold', () => {
  it('renders the reported quoted name after a Chinese character', () => {
    expect(parseStoryMarkdown('军用科技的**‘大猩猩’二代前置试验件**，')).toBe(
      '<p>军用科技的<strong>‘大猩猩’二代前置试验件</strong>，</p>\n',
    );
  });

  it.each(['‘名称’', '“名称”', '「名称」', '『名称』'])('supports Chinese opening quotes: %s', (name) => {
    expect(parseStoryMarkdown(`中文**${name}试件**。`)).toBe(`<p>中文<strong>${name}试件</strong>。</p>\n`);
  });

  it('keeps nested emphasis and code delimiters inside the intended bold span', () => {
    expect(parseStoryMarkdown('中文**‘名称’的*前置*试件及 `**` 标识**。')).toBe(
      '<p>中文<strong>‘名称’的<em>前置</em>试件及 <code>**</code> 标识</strong>。</p>\n',
    );
  });

  it('closes each quoted name separately', () => {
    expect(parseStoryMarkdown('中文**‘第一’试件**和**‘第二’试件**。')).toBe(
      '<p>中文<strong>‘第一’试件</strong>和<strong>‘第二’试件</strong>。</p>\n',
    );
  });

  it.each([
    'English**“quoted” text**.',
    'English **“quoted” text** and **normal bold**.',
    '中文**普通粗体**和*正常斜体*。',
    '中文*‘名称’斜体*。',
    '中文***‘名称’粗斜体***。',
    '中文__‘名称’下划线__。',
    '中文**（名称）试件**。',
    '中文**"名称"试件**。',
    '中文**‘名称’**后面',
    '中文**‘未闭合',
    '中文**‘仅一个闭合’星号*',
    '中文**‘跨段未闭合\n\n另一段**。',
    String.raw`中文\*\*‘字面’星号\*\*。`,
    String.raw`中文\**‘部分转义’星号**。`,
    '`中文**‘名称’试件**`',
    '```text\n中文**‘名称’试件**\n```',
    '    中文**‘名称’试件**',
    '[链接](https://example.com/中文**‘名称’试件**)',
    '<span title="中文**‘名称’试件**">属性</span>',
  ])('preserves standard parsing outside the supported case: %s', (source) => {
    expect(parseStoryMarkdown(source)).toBe(marked.parse(source, { async: false, gfm: true }));
  });

  it('leaves the global parser unchanged', () => {
    const source = '中文**‘名称’试件**。';
    parseStoryMarkdown(source);
    expect(marked.parse(source)).toBe(`<p>${source}</p>\n`);
  });

  it('preserves the story line-break option', () => {
    expect(parseStoryMarkdown('中文**‘名称’试件**\n下一行', true)).toBe(
      '<p>中文<strong>‘名称’试件</strong><br>下一行</p>\n',
    );
  });
});
