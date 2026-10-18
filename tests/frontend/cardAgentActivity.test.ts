import { describe, expect, it } from 'vitest';
import {
  editorActions,
  editorActivityBlocks,
  mergeEditorEvents,
  type EditorEvent,
} from '../../web/lib/cardAgentActivity';

describe('editor action timeline', () => {
  const started: EditorEvent = {
    seq: 2,
    kind: 'tool_started',
    payload: {
      action_id: 'read-1',
      tool: 'read_field',
      arguments: '{"path":"/description"}',
    },
  };
  const finished: EditorEvent = {
    seq: 4,
    kind: 'tool_finished',
    payload: {
      action_id: 'read-1',
      tool: 'read_field',
      status: 'completed',
      result: '角色描述',
      duration_ms: 30,
    },
  };
  it('merges replayed and live events without duplicate tool actions or losing completed results', () => {
    const events = mergeEditorEvents([finished], [started, finished, started]);
    expect(events.map((e) => e.seq)).toEqual([2, 4]);
    expect(editorActions(events, 'completed')).toEqual([
      {
        id: 'read-1',
        seq: 2,
        title: '读取字段',
        target: '/description',
        status: 'completed',
        arguments: '{"path":"/description"}',
        result: '角色描述',
        duration: 30,
        argumentsTruncated: undefined,
        resultTruncated: undefined,
      },
    ]);
  });
  it('retains failures and stops showing interrupted tools as running', () => {
    expect(editorActions([started], 'interrupted')[0].status).toBe('interrupted');
    const failed = {
      ...finished,
      payload: { ...finished.payload, status: 'error', result: '', detail: '字段不存在' },
    };
    expect(editorActions([started, failed], 'completed')[0]).toMatchObject({
      status: 'error',
      result: '字段不存在',
    });
  });
  it('distinguishes omitted log previews for arguments and results while preserving repeated calls', () => {
    const next = {
      ...started,
      seq: 5,
      payload: {
        ...started.payload,
        action_id: 'read-2',
        tool: 'read_skill',
        arguments: '{',
        arguments_truncated: true,
      },
    };
    const skillStarted = {
      ...started,
      payload: { ...started.payload, tool: 'read_skill', arguments: '{"path":"runtime-guide.md"}' },
    };
    const skillFinished = {
      ...finished,
      payload: { ...finished.payload, tool: 'read_skill', result_truncated: true },
    };
    const actions = editorActions([skillStarted, skillFinished, next], 'running');
    expect(actions).toHaveLength(2);
    expect(actions[0]).toMatchObject({ title: '读取 skill', status: 'completed', resultTruncated: true });
    expect(actions[0].argumentsTruncated).toBeUndefined();
    expect(actions[1]).toMatchObject({ status: 'running', argumentsTruncated: true, target: '' });
    expect(actions[1].resultTruncated).toBeUndefined();
  });
  it('shows operation comments while retaining remarks from legacy activity records', () => {
    const events = [
      {
        ...started,
        payload: {
          action_id: 'patch',
          tool: 'patch_draft',
          arguments: '{"card_ref":"C1","operations":[],"comment":"修改人物描述"}',
        },
      },
      {
        ...started,
        seq: 5,
        payload: {
          action_id: 'revert',
          tool: 'revert_draft',
          arguments: '{"card_ref":"C1","comment":"恢复原设定"}',
        },
      },
      {
        ...started,
        seq: 8,
        payload: {
          action_id: 'legacy',
          tool: 'patch_draft',
          arguments: '{"card_ref":"C1","reason":"旧操作备注"}',
        },
      },
    ];
    const actions = editorActions(events, 'completed');
    expect(actions.map((action) => action.target)).toEqual(['修改人物描述', '恢复原设定', '旧操作备注']);
    expect(actions[2].arguments).toContain('"reason"');
  });
});

describe('public editor intermediate replies', () => {
  const event = (seq: number, kind: string, payload: Record<string, any> = {}): EditorEvent => ({
    seq,
    kind,
    payload,
  });
  const source = [
    event(1, 'model_call', { call: 1 }),
    event(2, 'text', { text: '我会先检查。' }),
    event(3, 'text', { text: '\n完整中间说明。' }),
    event(4, 'provider_response', {
      native: { items: [{ type: 'reasoning', summary: '不可展示的推理', encrypted_content: 'opaque' }] },
    }),
    event(5, 'tool_started', { action_id: 'read', tool: 'overview', arguments: '{}' }),
    event(6, 'tool_finished', { action_id: 'read', tool: 'overview', status: 'completed', result: '{}' }),
    event(7, 'model_call', { call: 2 }),
    event(8, 'text', { text: '最终回复。' }),
  ];
  it('keeps public prose between tool groups after completion without duplicating the final reply', () => {
    const blocks = editorActivityBlocks(source, 'completed');
    expect(blocks.map((block) => block.kind)).toEqual(['tools', 'assistant', 'tools']);
    expect(blocks[1]).toMatchObject({ text: '我会先检查。\n完整中间说明。', status: 'intermediate' });
    expect(JSON.stringify(blocks)).not.toContain('最终回复');
    expect(JSON.stringify(blocks)).not.toContain('不可展示的推理');
    expect(JSON.stringify(blocks)).not.toContain('opaque');
  });
  it('does not clear earlier text when a new request begins, including requests with no text', () => {
    const before = editorActivityBlocks(source.slice(0, 3), 'running');
    const next = editorActivityBlocks(source.slice(0, 7), 'running');
    expect(before[1]).toMatchObject({ status: 'streaming' });
    expect(next[1]).toMatchObject({
      id: before[1].id,
      text: '我会先检查。\n完整中间说明。',
      status: 'intermediate',
    });
    const live = editorActivityBlocks(source, 'running');
    expect(live.at(-1)).toMatchObject({ text: '最终回复。', status: 'streaming' });
  });
  it('retains partial user-visible output after interruption and deduplicates replayed chunks', () => {
    const merged = mergeEditorEvents(source, source.slice(0, 5));
    const blocks = editorActivityBlocks(merged, 'cancelled');
    expect(blocks.filter((block) => block.kind === 'assistant')).toHaveLength(2);
    expect(blocks.at(-1)).toMatchObject({ text: '最终回复。', status: 'interrupted' });
  });
  it('handles ordinary text-only and tool-only responses without phase metadata', () => {
    const plain = [event(1, 'model_call', { call: 1 }), event(2, 'text', { text: '普通 API 回复' })];
    expect(editorActivityBlocks(plain, 'completed').every((block) => block.kind === 'tools')).toBe(true);
    const tools = source.filter((item) => item.kind !== 'text');
    expect(editorActivityBlocks(tools, 'completed')).toHaveLength(1);
  });
});
