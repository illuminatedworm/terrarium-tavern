import { describe, expect, it } from 'vitest';
import { splitMvuPanel } from '../../web/lib/storyPanels';

const panel = `<details id="terminal" open><summary>诊断终端</summary><div><span id="hp"></span></div></details>
<script>document.getElementById('hp').textContent = Mvu.getMvuData({type:'chat'}).stat_data.hp;</script>`;

describe('MVU panel separation', () => {
  it('extracts the appended details interface without moving the story into the sidebar', () => {
    const narrative = '客人推门进来。\n\n医生检查义体。\n';
    expect(splitMvuPanel(narrative + panel)).toEqual({ panel, narrative });
  });

  it('retains prose after an unfenced panel even without a blank line', () => {
    const text = `开头。\n\n${panel}\n结尾。\n\n下一段。`;
    expect(splitMvuPanel(text)).toEqual({ panel, narrative: '开头。\n\n\n结尾。\n\n下一段。' });
  });

  it('keeps nested details, styles, comments, void tags, and markup inside script strings', () => {
    const html = `<style>#terminal{color:cyan}</style><!-- panel -->
<details><summary>终端</summary><details><summary>扫描</summary><div title="a > b"><input><br>状态</div></details></details>
<script>const example = '<div>example</div>'; const state = getAllVariables().stat_data;</script>`;
    expect(splitMvuPanel(`前文。\n\n${html}\n后文。`)).toEqual({
      panel: html,
      narrative: '前文。\n\n\n后文。',
    });
  });

  it('moves fenced HTML and preserves other code and prose on both sides', () => {
    const before = '```text\n普通代码\n```\n\n正文。\n\n';
    const after = '\n末尾正文。';
    expect(splitMvuPanel(`${before}\`\`\`html\n${panel}\n\`\`\`\n${after}`)).toEqual({
      panel,
      narrative: before + '\n' + after,
    });
  });

  it('preserves original CRLF and CR in narrative while tracking normalized HTML offsets', () => {
    const before = '第一段。\r\n\r\n第二段。\r\r';
    const after = '\r\n结束。';
    expect(splitMvuPanel(before + panel.replace(/\n/g, '\r\n') + after)).toEqual({
      panel,
      narrative: before + after,
    });
  });

  it('keeps unrelated HTML and extracts each separate MVU interface', () => {
    const text = `<div>叙事 HTML</div>\n\n中间。\n\n${panel}\n后续。\n\n${panel}`;
    expect(splitMvuPanel(text)).toEqual({
      panel: panel + '\n' + panel,
      narrative: '<div>叙事 HTML</div>\n\n中间。\n\n\n后续。\n\n',
    });
  });

  it.each([
    '普通正文提到了 stat_data 和 getMvuData。',
    '<div>普通交互</div><script>console.log("hello")</script>',
    '```javascript\nconst html = "<script>getMvuData()</script>";\n```',
    '<details><summary>未闭合的面板</summary><script>getMvuData()</script>',
  ])('leaves non-panels and incomplete HTML intact: %s', (text) => {
    expect(splitMvuPanel(text)).toEqual({ panel: '', narrative: text });
  });
});
