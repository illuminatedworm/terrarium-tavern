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
  set_opening: '迁移开场',
  validate_draft: '验证草稿',
  read_skill: '读取适配 skill',
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
        title: '分析草稿',
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
            args.reason ??
            args.resource ??
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
