import type { Channel } from './channels';
import type { ConversationModelSelection } from './settingsDocument';

export interface AdvisorRun {
  id: string;
  status: string;
  response: string;
  error: string | null;
  resumable: boolean;
  request: { text: string; compact: boolean };
}
export interface AdvisorExchange {
  seq: number;
  user_text: string;
  response: string;
  stale: boolean;
  trace: Array<{
    role: string;
    content: string;
    tool_calls?: Array<{ function: { name: string; arguments: string } }>;
  }>;
}
export interface AdvisorState {
  revision: number;
  options: {
    recent_exchanges?: number | null;
    model_selection?: ConversationModelSelection | null;
    context_strategy?: string | null;
  };
  effective: {
    recent_exchanges: number;
    range_source: string;
    model_selection: ConversationModelSelection | null;
    channel: Channel;
    context_strategy: string;
  };
  exchanges: AdvisorExchange[];
  run: AdvisorRun | null;
  context: { summary?: string; through?: number; strategy?: string };
}
export interface AdvisorEvent {
  seq: number;
  kind: string;
  payload: Record<string, any>;
}
export const toolLabels: Record<string, string> = {
  search_history: '查找剧情',
  read_history: '读取剧情',
  recall_lore: '召回世界书',
  find_lore_for_edit: '查找世界书',
  read_lore_entry: '读取世界书',
  read_character: '读取人物资料',
  read_state: '读取当前状态',
  search_notes: '查找笔记',
  read_note: '读取笔记',
  search_memory: '查找记忆',
  read_story_context: '读取故事背景',
  search_consultation: '查找旧咨询',
  read_consultation: '读取旧咨询',
  web_search: '搜索网页',
  read_webpage: '读取网页',
  update_turn_preference: '准备修改本轮偏好',
};
export function advisorRunning(run?: AdvisorRun | null) {
  return !!run && ['queued', 'running'].includes(run.status);
}

/** Reconnected SSE may repeat events; old observations never mutate a new run. */
export function eventCursor() {
  let cursor = 0;
  return {
    accept(event: AdvisorEvent) {
      if (event.seq <= cursor) return false;
      cursor = event.seq;
      return true;
    },
  };
}
