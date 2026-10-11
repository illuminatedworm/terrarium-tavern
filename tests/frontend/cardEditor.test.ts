import { describe, expect, it } from 'vitest';
import { editorTimeline, mentionCard, type EditorMessage, type EditorRun } from '../../web/lib/cardEditor';
import { appendSkillRequest, editorSkillPresets } from '../../web/lib/cardEditorSkills';
import { previewCardData, previewLore } from '../../web/lib/cardPreview';

describe('explicit card references', () => {
  it('replaces only the active mention and preserves earlier prose', () => {
    expect(mentionCard('保留 @C1 的人设，参考 @', 'C2')).toBe('保留 @C1 的人设，参考 @C2 ');
    expect(mentionCard('根据这两张卡', 'C3')).toBe('根据这两张卡 @C3 ');
    expect(mentionCard('@书店', 'C1')).toBe('@C1 ');
  });
});

const run = (id: string, status = 'completed'): EditorRun => ({
  id,
  status,
  request: { text: '要求' },
  response: '',
  error: null,
});
const message = (ref: string, role: string, run: string): EditorMessage => ({ ref, role, run, content: ref });
const keys = (messages: EditorMessage[], runs: EditorRun[]) =>
  editorTimeline(messages, runs).map((item) => item.key);

describe('editor conversation chronology', () => {
  it('places each run immediately above its own assistant reply, even when text repeats', () => {
    const messages = [
      message('H1', 'user', 'a'),
      message('H2', 'assistant', 'a'),
      message('H3', 'user', 'b'),
      message('H4', 'assistant', 'b'),
    ];
    expect(keys(messages, [run('a'), run('b')])).toEqual([
      'message:H1',
      'run:a',
      'message:H2',
      'message:H3',
      'run:b',
      'message:H4',
    ]);
  });
  it('keeps cancelled, failed and resumed attempts in their original exchange', () => {
    const messages = [
      message('H1', 'user', 'a'),
      message('H2', 'assistant', 'c'),
      message('H3', 'user', 'd'),
      message('H4', 'assistant', 'd'),
    ];
    const runs = [
      run('a', 'cancelled'),
      { ...run('b', 'error'), request: { text: '', resume_run_id: 'a' } },
      { ...run('c'), request: { text: '', resume_run_id: 'b' } },
      run('d'),
    ];
    expect(keys(messages, runs)).toEqual([
      'message:H1',
      'run:a',
      'run:b',
      'run:c',
      'message:H2',
      'message:H3',
      'run:d',
      'message:H4',
    ]);
  });
  it('retains live runs without an answer and older messages without a matching run', () => {
    const messages = [message('H1', 'assistant', 'legacy'), message('H2', 'user', 'live')];
    expect(keys(messages, [run('live', 'running')])).toEqual(['message:H1', 'message:H2', 'run:live']);
    expect(keys([], [run('resume', 'running')])).toEqual(['run:resume']);
  });
  it('does not duplicate process records for multiple messages owned by one run', () => {
    const messages = [
      message('H1', 'user', 'a'),
      message('H2', 'assistant', 'a'),
      message('H3', 'assistant', 'a'),
    ];
    expect(keys(messages, [run('a')])).toEqual(['message:H1', 'run:a', 'message:H2', 'message:H3']);
    expect(messages.map((item) => item.ref)).toEqual(['H1', 'H2', 'H3']);
  });
});

describe('built-in skill requests', () => {
  it('targets the selected card and preserves unsent requirements verbatim', () => {
    const input = '保留 @C1 的外貌。\n不要修改玩家身份。';
    const result = appendSkillRequest(input, 'C2', editorSkillPresets[1]);
    expect(result).toBe(`${input}\n\n@C2 ${editorSkillPresets[1].prompt}`);
    expect(appendSkillRequest('', 'C1', editorSkillPresets[0])).toBe(`@C1 ${editorSkillPresets[0].prompt}`);
  });
});

describe('current draft reading preview', () => {
  it('uses the current wrapped document and preserves complete object-style worldbook entries', () => {
    const entry = {
      comment: '灯塔',
      content: '长篇设定\n'.repeat(500) + '完整末尾',
      enabled: true,
      constant: true,
      extensions: { terrarium: { audience: 'tracker_mvu' } },
    };
    const document = {
      name: '旧的顶层镜像',
      spec: 'chara_card_v2',
      data: { name: '当前草稿', character_book: { entries: { 'key/a': entry } } },
    };
    const before = JSON.stringify(document);
    expect(previewCardData(document).name).toBe('当前草稿');
    expect(previewLore(document)).toEqual([
      {
        key: 'key/a',
        title: '灯塔',
        content: entry.content,
        enabled: true,
        stateLabel: '',
        trigger: '常驻',
        audience: 'tracker_mvu',
        purpose: '变量规则',
        keywords: [],
      },
    ]);
    expect(JSON.stringify(document)).toBe(before);
  });
  it('handles absent books, legacy switches, names and normalized extension overrides', () => {
    expect(previewLore({ character_book: null })).toEqual([]);
    expect(
      previewLore({
        character_book: {
          entries: [
            { name: '停用项', disable: true, key: ['雾港'] },
            { content: '默认标题', constant: false, extensions: { constant: true, enabled: false } },
          ],
        },
      }),
    ).toMatchObject([
      { title: '停用项', enabled: false, keywords: ['雾港'] },
      { title: '条目 2', enabled: false, trigger: '常驻' },
    ]);
  });
});
