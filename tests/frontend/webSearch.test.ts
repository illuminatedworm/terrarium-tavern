import { describe, expect, it } from 'vitest';
import { editorActions, editorActivityBlocks, type EditorEvent } from '../../web/lib/cardAgentActivity';
import { webActivityLabel, webSources } from '../../web/lib/webSearch';

describe('web search activities and sources', () => {
  it('shows the actual backend, authentication and fallback reason', () => {
    expect(
      webActivityLabel({ service: 'tavily', auth_mode: 'keyless', status: 'error', reason: '服务限流' }),
    ).toBe('Tavily · 免 key · 失败 · 服务限流');
    const events: EditorEvent[] = [
      { seq: 1, kind: 'web_activity', payload: { action_id: 'web-1', service: 'exa', status: 'started' } },
      { seq: 2, kind: 'web_activity', payload: { action_id: 'web-1', service: 'exa', status: 'completed' } },
    ];
    expect(editorActions(events, 'completed')).toHaveLength(1);
    expect(editorActions(events, 'completed')[0].status).toBe('completed');
  });
  it('drops only the failed request text when cloud search falls back', () => {
    const events: EditorEvent[] = [
      { seq: 1, kind: 'model_call', payload: { call: 1 } },
      { seq: 2, kind: 'text', payload: { text: '保留前次说明' } },
      { seq: 3, kind: 'tool_started', payload: { action_id: 'read', tool: 'read_field', arguments: '{}' } },
      { seq: 4, kind: 'model_call', payload: { call: 2 } },
      { seq: 5, kind: 'text', payload: { text: '丢弃未完成搜索' } },
      { seq: 6, kind: 'response_restarted', payload: {} },
      { seq: 7, kind: 'text', payload: { text: '本地搜索结果' } },
    ];
    const blocks = editorActivityBlocks(events, 'running');
    const text = blocks
      .filter((b) => b.kind === 'assistant')
      .map((b) => b.text)
      .join('');
    expect(text).toContain('保留前次说明');
    expect(text).toContain('本地搜索结果');
    expect(text).not.toContain('丢弃未完成搜索');
  });
  it('deduplicates citations and rejects executable or credential-bearing URLs', () => {
    const source = { url: 'https://example.org', title: '来源' };
    expect(
      webSources([
        {
          web_sources: [
            source,
            source,
            { url: 'javascript:alert(1)', title: 'bad' },
            { url: 'https://user:key@example.org', title: 'bad' },
          ],
        },
      ]),
    ).toEqual([source]);
  });
});
