import { describe, expect, it } from 'vitest';
import { editorActions, mergeEditorEvents, type EditorEvent } from '../../web/lib/cardAgentActivity';

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
        truncated: undefined,
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
  it('handles intentionally truncated arguments and preserves repeated calls to the same tool', () => {
    const next = {
      ...started,
      seq: 5,
      payload: { ...started.payload, action_id: 'read-2', arguments: '{', arguments_truncated: true },
    };
    const actions = editorActions([started, finished, next], 'running');
    expect(actions).toHaveLength(2);
    expect(actions[1]).toMatchObject({ status: 'running', truncated: true, target: '' });
  });
});
