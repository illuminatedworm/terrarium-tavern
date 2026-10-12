export interface EditorEvent {
  seq: number;
  kind: string;
  payload: Record<string, any>;
  created?: number;
}
export interface EditorAction {
  id: string;
  seq: number;
  title: string;
  target: string;
  status: string;
  arguments?: string;
  result?: string;
  truncated?: boolean;
  duration?: number;
}
const labels: Record<string, string> = {
  overview: '查看角色卡概览',
  read_field: '读取字段',
  search_entries: '搜索世界书',
  patch_draft: '修改草稿',
  set_text_field: '编辑文本字段',
  set_opening: '编辑开场',
  validate_draft: '验证草稿',
  read_skill: '读取适配 skill',
  create_draft: '创建草稿',
  configure_card: '记录目标模式',
  revert_draft: '撤销本轮卡片修改',
  test_draft: '本地运行验证',
  write_handoff: '更新交接笔记',
  new_context: '保存交接笔记并换窗',
  search_history: '搜索编辑历史',
  read_history: '读取完整历史',
  list_personas: '查看用户角色目录',
  read_persona: '读取用户角色',
  create_persona: '新增用户角色',
  update_persona: '修改用户角色',
};
export function mergeEditorEvents(current: EditorEvent[], incoming: EditorEvent[]) {
  const events = new Map(current.map((event) => [event.seq, event]));
  for (const event of incoming) events.set(event.seq, event);
  return [...events.values()].sort((a, b) => a.seq - b.seq);
}
export function editorActions(events: EditorEvent[], runStatus: string): EditorAction[] {
  const actions = new Map<string, EditorAction>();
  for (const event of events) {
    const p = event.payload;
    if (event.kind === 'model_call') {
      actions.set(`model-${event.seq}`, {
        id: `model-${event.seq}`,
        seq: event.seq,
        title: '思考与处理',
        target: `第 ${p.call} 次模型请求`,
        status: 'requested',
      });
    } else if (event.kind === 'tool_started' || event.kind === 'tool_finished') {
      const id = p.action_id;
      const action: EditorAction = actions.get(id) || {
        id,
        seq: event.seq,
        title: labels[p.tool] || p.tool,
        target: '',
        status: 'running',
      };
      if (event.kind === 'tool_started') {
        action.arguments = p.arguments;
        action.truncated = p.arguments_truncated;
        try {
          const args = JSON.parse(p.arguments);
          action.target =
            args.path ??
            args.query ??
            args.comment ??
            args.reason ??
            args.resource ??
            args.persona_ref ??
            args.name ??
            (typeof args.index === 'number' ? `开场 ${args.index + 1}` : '');
        } catch {
          /* A long argument preview may be intentionally truncated. */
        }
      } else {
        action.status = p.status;
        action.result = p.result || p.detail;
        action.duration = p.duration_ms;
        action.truncated ||= p.result_truncated;
      }
      actions.set(id, action);
    }
  }
  for (const action of actions.values()) {
    if (action.status === 'running' && !['queued', 'running'].includes(runStatus)) {
      action.status = runStatus === 'completed' ? 'unknown' : runStatus;
    }
  }
  return [...actions.values()].sort((a, b) => a.seq - b.seq);
}
export function actionStatus(status: string) {
  return (
    (
      {
        running: '执行中',
        queued: '等待中',
        completed: '完成',
        error: '失败',
        cancelled: '已取消',
        interrupted: '已中断',
        unknown: '结果未记录',
        requested: '已请求',
      } as Record<string, string>
    )[status] || status
  );
}

export type EditorActivityBlock =
  | { kind: 'tools'; id: string; actions: EditorAction[] }
  | { kind: 'assistant'; id: string; text: string; status: 'intermediate' | 'streaming' | 'interrupted' };

/** Text is public provider output, never reasoning/debug payloads. Request/tool
 * boundaries work for ordinary APIs as well as Responses commentary messages.
 * Only the successful final request moves to the committed reply below the run.
 */
export function editorActivityBlocks(events: EditorEvent[], runStatus: string): EditorActivityBlock[] {
  const ordered = mergeEditorEvents([], events);
  type TextStep = { id: string; seq: number; text: string; hasTools: boolean };
  const texts: TextStep[] = [];
  let current: TextStep | undefined;
  for (const event of ordered) {
    if (event.kind === 'model_call') current = undefined;
    else if (event.kind === 'text' && typeof event.payload.text === 'string') {
      if (!current) {
        current = { id: `text-${event.seq}`, seq: event.seq, text: '', hasTools: false };
        texts.push(current);
      }
      current.text += event.payload.text;
    } else if (event.kind === 'tool_started' && current) current.hasTools = true;
  }
  const live = ['queued', 'running'].includes(runStatus);
  const entries = [
    ...editorActions(ordered, runStatus).map((action) => ({ seq: action.seq, action })),
    ...texts
      .filter(
        (text) => text.text.trim() && !(runStatus === 'completed' && text === current && !text.hasTools),
      )
      .map((text) => ({ seq: text.seq, text })),
  ].sort((a, b) => a.seq - b.seq);
  const blocks: EditorActivityBlock[] = [];
  for (const entry of entries) {
    if ('action' in entry) {
      const previous = blocks.at(-1);
      if (previous?.kind === 'tools') previous.actions.push(entry.action);
      else blocks.push({ kind: 'tools', id: entry.action.id, actions: [entry.action] });
    } else {
      const unfinished = entry.text === current && !entry.text.hasTools;
      blocks.push({
        kind: 'assistant',
        id: entry.text.id,
        text: entry.text.text,
        status: unfinished ? (live ? 'streaming' : 'interrupted') : 'intermediate',
      });
    }
  }
  return blocks;
}
