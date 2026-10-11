import { describe, expect, it } from 'vitest';
import { responseData, storyPresentation } from '../../web/lib/storyPresentation';

// Like imported ST status interfaces: <head>/<body>, blank lines, indented CSS
// and script containing markup. Markdown's indented-code heuristic cuts this up.
const document = `<head>
<style>
:root { --accent: cyan; }

        * { box-sizing: border-box; }
        body { color: var(--accent); }
    </style>
    <script>
        const markup = '<div>not the panel root</div>';
        const state = getAllVariables().stat_data;
    </script>
</head>

<body>
    <div id="monitor">

        <div class="header">状态栏</div>
    </div>
</body>`;

describe('normalized story presentation data', () => {
  it('keeps HTML with optional descendant end tags and retains following narrative', () => {
    const html = '<table><tr><td>生命<td>45</table>';
    expect(storyPresentation(`前文。\n\n${html}\n后文。`, 'assistant')).toEqual({
      body: [
        { format: 'markdown', text: '前文。\n\n' },
        { format: 'html', text: html },
        { format: 'markdown', text: '\n后文。' },
      ],
      panel: [],
    });
  });
  it.each(['', 'html'])('keeps a complete HTML document from a %s fence intact', (lang) => {
    const source = `前文。\n\n\`\`\`${lang}\n${document}\n\`\`\`\n后文。`;
    const presentation = storyPresentation(source, 'assistant');
    expect(presentation.panel).toEqual([{ format: 'html', text: document }]);
    expect(presentation.body.every((part) => part.format === 'markdown')).toBe(true);
    const prose = presentation.body.map((part) => part.text).join('');
    expect(prose).toContain('前文。');
    expect(prose).toContain('后文。');
    expect(prose).not.toContain('box-sizing');
  });

  it('normalizes unfenced documents to the same HTML payload', () => {
    const presentation = storyPresentation(`前文。\n\n${document}\n后文。`, 'assistant');
    expect(presentation.panel).toEqual([{ format: 'html', text: document }]);
    expect(presentation.body.map((part) => part.text).join('')).toEqual('前文。\n\n\n后文。');
  });

  it('preserves a non-MVU raw document as HTML without selecting indented CSS or JS', () => {
    const plain = document.replace('getAllVariables().stat_data', '{}');
    expect(storyPresentation(plain, 'assistant')).toEqual({
      body: [{ format: 'html', text: plain }],
      panel: [],
    });
  });

  it('normalizes API and event message data without changing stored fields or variable values', () => {
    const message = {
      id: 'reply',
      role: 'assistant',
      raw: '原文',
      model: '模型文本',
      display: `\`\`\`html\n${document}\n\`\`\``,
      snapshot: { state: { stat_data: { hp: 45 } } },
    };
    const source = { messages: [message], pending: { messages: [message] } };
    const result = responseData(source);
    expect((result.messages[0] as any).presentation.panel).toEqual([{ format: 'html', text: document }]);
    expect((result.pending.messages[0] as any).presentation).toEqual(
      (result.messages[0] as any).presentation,
    );
    expect(result.messages[0]).toMatchObject(message);
    expect((message as any).presentation).toBeUndefined();
    expect(result.messages[0].snapshot.state.stat_data).toEqual({ hp: 45 });
  });

  it('respects a deliberately empty display produced by hiding rules', () => {
    const result = responseData({
      role: 'assistant',
      raw: '<initvar>hp: 45</initvar>',
      display: '',
      snapshot: {},
    });
    expect((result as any).presentation).toEqual({ body: [{ format: 'markdown', text: '' }], panel: [] });
  });

  it('preserves prose and every CSS-only HTML block without requiring an MVU script', () => {
    const first =
      '<style>.status{color:cyan}</style><div class="status">余额：{{format_message_variable::stat_data.Money}}</div>';
    const second = '<details><summary>属性</summary>生命：{{get_message_variable::stat_data.hp.0}}</details>';
    const source = `开头。\n\n\`\`\`html\n${first}\n\`\`\`\n\n中间。\n\n\`\`\`html\n${second}\n\`\`\`\n\n结尾。`;
    const view = storyPresentation(source, 'assistant');
    expect(view.panel).toEqual([]);
    expect(view.body.filter((part) => part.format === 'html').map((part) => part.text)).toEqual([
      first,
      second,
    ]);
    expect(
      view.body
        .filter((part) => part.format === 'markdown')
        .map((part) => part.text)
        .join(''),
    ).toMatch(/开头。[\s\S]*中间。[\s\S]*结尾。/);
  });
});
