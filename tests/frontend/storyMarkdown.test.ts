import { describe, expect, it } from 'vitest';
import { marked } from 'marked';
import { parseStoryMarkdown } from '../../web/lib/storyMarkdown';

describe('story Markdown East Asian emphasis', () => {
  it('renders quoted names next to Chinese prose without swallowing later bold spans', () => {
    expect(parseStoryMarkdown('军用科技的**‘大猩猩’二代前置试验件**，')).toBe(
      '<p>军用科技的<strong>‘大猩猩’二代前置试验件</strong>，</p>\n',
    );
    expect(parseStoryMarkdown('传说秘籍——**「G.Bible」**吗？我们一定能做出**满分神作**。')).toBe(
      '<p>传说秘籍——<strong>「G.Bible」</strong>吗？我们一定能做出<strong>满分神作</strong>。</p>\n',
    );
    expect(parseStoryMarkdown('中文**‘名称’**后面')).toBe('<p>中文<strong>‘名称’</strong>后面</p>\n');
  });

  it('recognizes punctuation at Chinese, Japanese, and Korean emphasis boundaries', () => {
    const source = [
      '他说**找到了！**随后起身',
      'かな*「引用」*です',
      '**(Markdown)**을',
      'a**（x）**b',
      '𰻞***(麺)***𰻞',
    ].join('\n\n');
    expect(parseStoryMarkdown(source)).toBe(
      '<p>他说<strong>找到了！</strong>随后起身</p>\n' +
        '<p>かな<em>「引用」</em>です</p>\n' +
        '<p><strong>(Markdown)</strong>을</p>\n' +
        '<p>a<strong>（x）</strong>b</p>\n' +
        '<p>𰻞<em><strong>(麺)</strong></em>𰻞</p>\n',
    );
  });

  it('keeps nested emphasis, code, and links inside the intended bold span', () => {
    expect(
      parseStoryMarkdown('中文**（前*置*及 `」**中文` 和 [名](https://example.com/废」**章节)）**随后'),
    ).toBe(
      '<p>中文<strong>（前<em>置</em>及 <code>」**中文</code> 和 ' +
        '<a href="https://example.com/%E5%BA%9F%E3%80%8D**%E7%AB%A0%E8%8A%82">名</a>）</strong>随后</p>\n',
    );
  });

  it.each([
    'English**“quoted” text**.',
    'English **“quoted” text** and **normal bold**.',
    '中文**普通粗体**和*正常斜体*。',
    '中文__‘名称’下划线__。',
    '**中文 **接着\n\n語** 強調**語\n\na**😀**b',
    '中文**‘未闭合',
    '中文**‘仅一个闭合’星号*',
    '中文**‘跨段未闭合\n\n另一段**。',
    String.raw`中文\*\*‘字面’星号\*\*。`,
    String.raw`中文\**‘部分转义’星号*。`,
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
