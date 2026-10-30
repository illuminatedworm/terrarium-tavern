export interface AgentActivity {
  id: string;
  order: number;
  event_seq?: number;
  kind: 'tool' | 'assistant';
  is_story?: boolean;
  name?: string;
  summary: string;
  status: 'running' | 'returned' | 'error' | 'interrupted';
  details?: Partial<Record<'arguments' | 'result' | 'content', string>>;
}

export function activityText(value: unknown): string {
  if (value == null) return '';
  if (typeof value === 'string') {
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      return value;
    }
  }
  return JSON.stringify(value, null, 2);
}

export function mergeActivity(...groups: AgentActivity[][]): AgentActivity[] {
  const items = new Map<string, AgentActivity>();
  for (const group of groups)
    for (const item of group) {
      if ((items.get(item.id)?.event_seq ?? 0) <= (item.event_seq ?? 0)) items.set(item.id, item);
    }
  return Array.from(items.values()).sort((a, b) => a.order - b.order || a.id.localeCompare(b.id));
}

/** SSE can replay; native activity ids pair starts/results without mixing turns. */
export function liveActivity(events: any[], turnId: string, status: string): AgentActivity[] {
  const items = new Map<string, AgentActivity>();
  const seen = new Set<number>();
  for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
    if ((event.turn && event.turn !== turnId) || seen.has(event.seq)) continue;
    seen.add(event.seq);
    const p = event.payload || {};
    const id = p.activity_id || `event-${event.seq}`;
    const order = p.order ?? event.seq * 10;
    if (event.kind === 'full_agent_assistant') {
      items.set(id, {
        id,
        event_seq: event.seq,
        order,
        kind: 'assistant',
        is_story: !!p.is_story,
        summary: String(p.content).slice(0, 180),
        status: 'returned',
        details: { content: String(p.content) },
      });
    } else if (['full_agent_tool_started', 'full_agent_tool'].includes(event.kind)) {
      let args: any = p.arguments;
      try {
        if (typeof args === 'string') args = JSON.parse(args);
      } catch {
        args = {};
      }
      const summary =
        p.name === 'write_plot'
          ? '包含未来剧情，展开查看'
          : ['roleplay', 'kemini_emit_story'].includes(p.name)
            ? '正文交付；展开查看原始参数'
            : p.name === 'finish_turn'
              ? '申请正文回调与统一校验'
              : ['query', 'key', 'ref', 'path', 'reason', 'content'].map((key) => args?.[key]).find(Boolean);
      const finished = event.kind === 'full_agent_tool';
      items.set(id, {
        id,
        event_seq: event.seq,
        order,
        kind: 'tool',
        name: p.name,
        summary: String(summary || '')
          .replace(/\s+/g, ' ')
          .slice(0, 140),
        status: finished
          ? p.result && typeof p.result === 'object' && 'ok' in p.result && p.result.ok === false
            ? 'error'
            : 'returned'
          : 'running',
        details: {
          arguments: activityText(p.arguments),
          ...(finished ? { result: activityText(p.result) } : {}),
        },
      });
    }
  }
  return mergeActivity(Array.from(items.values())).map((item) =>
    item.status === 'running' && !['running', 'queued'].includes(status)
      ? { ...item, status: 'interrupted' }
      : item,
  );
}

export const toolLabels: Record<string, string> = {
  recall_lore: '检索并读取世界书',
  find_lore_for_edit: '定位世界书条目',
  read_lore_entry: '读取单条世界书',
  search_history: '搜索历史',
  read_history: '读取历史',
  write_handoff: '更新交接笔记',
  write_plot: '更新剧情规划',
  new_context: '切换工作窗口',
  search_notes: '搜索笔记',
  read_note: '读取笔记',
  write_note: '保存笔记',
  archive_note: '归档笔记',
  read_state: '读取状态',
  patch_state: '修改状态',
  roleplay: '交付正文',
  finish_turn: '申请收尾',
  kemini_emit_story: '交付实验正文',
  read_character: '读取角色资料',
  revise_content: '修改设定',
  set_retrieval_query: '设置检索线索',
  start_novel_research: '委派原著调查',
  list_novel_research: '查看调查者',
  message_novel_research: '联系原著调查者',
  wait_novel_research: '等待原著调查',
  resume_novel_research: '恢复原著调查',
  destroy_novel_research: '销毁原著调查者',
};
