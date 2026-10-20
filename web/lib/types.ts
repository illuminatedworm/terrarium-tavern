/** Response shapes of the story API (`src/tavern/storage/store.py`).
 *
 * Character documents, snapshots and MVU state are user-authored JSON with no fixed
 * schema, so they stay `JsonObject`; everything the app itself defines is typed. */
import type { Channel } from './channels';
import type { ConversationCacheUsage } from './editorContextStats';
import type { ConversationModelSelection } from './settingsDocument';
import type { StoryPresentation } from './storyPresentation';

export type JsonObject = Record<string, any>;

export type AgentMode = 'half_agent' | 'full_agent';
export type TurnStatus =
  | 'queued'
  | 'running'
  | 'awaiting_review'
  | 'committed'
  | 'error'
  | 'interrupted'
  | 'cancelled'
  | 'rejected';

/** `Store.card()`; editor surfaces add client-only `_`-prefixed markers. */
export interface CardRecord {
  id: string;
  name: string;
  original: string;
  container: string;
  resources: Record<string, string>;
  permission: number;
  document: JsonObject;
  report: JsonObject & { variables?: string };
  revision: number;
  session_id?: string;
  _draftOnly?: boolean;
  _editorKey?: string;
  _workspaceId?: string;
}

/** `Store.sessions()` rows: raw columns plus the card name. */
export interface SessionSummary {
  id: string;
  title: string;
  name: string;
  persona: string;
  created: number;
  agent_mode: AgentMode;
  channel_origin: Channel | null;
  kemini: number;
  card_data: string | null;
  revision: number;
}

export interface StoryMessageRecord {
  id: string;
  parent: string | null;
  role: 'user' | 'assistant';
  raw: string;
  model: string;
  display: string;
  snapshot: JsonObject;
  created: number;
  versions?: string[];
  version_index?: number;
  /** Added client-side by `api()` for rendering. */
  presentation?: StoryPresentation;
}

/** `Store.turn()`. */
export interface TurnRecord {
  id: string;
  session: string;
  status: TurnStatus;
  /** The submitted `TurnRequest`; scripts may add generate options. */
  request: JsonObject & {
    expected_revision: number;
    update_variables?: boolean;
    scene_refresh?: string | null;
  };
  draft: string;
  error: string | null;
  pending: JsonObject | null;
  resumable: boolean;
  retry_head: string | null;
}

/** `Store.session()`: one opened story with its current branch. */
export interface Session {
  id: string;
  card: string;
  head: string;
  revision: number;
  title: string;
  persona: string;
  persona_data: JsonObject;
  agent_mode: AgentMode;
  channel_origin: Channel | null;
  kemini: boolean;
  policy: string | null;
  policy_effective: string;
  recipe: JsonObject;
  /** `{}` until the conversation picks a model of its own. */
  model_selection: ConversationModelSelection | { provider_id?: undefined };
  context_strategy: string;
  compact_auto: boolean;
  update_variables: boolean;
  plot_enabled: boolean;
  disabled_tools: string[];
  turn_preference: string;
  lore_injection_mode: string;
  cache_breakpoint_auto: boolean;
  cache_breakpoint_message: string | null;
  cache_breakpoint_effective_id: string | null;
  cache_breakpoint_follow: JsonObject;
  cache_breakpoint_reason: string;
  card_data: CardRecord | null;
  messages: StoryMessageRecord[];
  snapshot: JsonObject;
  memory_view: JsonObject;
  turns: Pick<TurnRecord, 'id' | 'status' | 'draft' | 'error'>[];
  /** Only on the response to a message edit. */
  edit_affected_memory?: boolean;
}

/** `GET /card-edits`: drafts kept after their library card was deleted. */
export interface OrphanDraft {
  id: string;
  name: string;
}

/** `GET /card-author/conversations` rows. */
export interface CardConversationSummary {
  id: string;
  title: string;
  status: string;
  resumable: boolean;
  revision: number;
  reference_count?: number;
  card_count?: number;
  channel_origin: Channel | null;
}

/** `GET /sessions/{id}/branches`: every message of the story, across branches. */
export type BranchMessage = Pick<StoryMessageRecord, 'id' | 'parent' | 'role' | 'raw' | 'created'>;

/** `GET /sessions/{id}/memory/rebuild-estimate`. */
export interface MemoryRebuildEstimate {
  estimated_batches: number;
  review_max_calls: number;
  message: string;
}

/** A user input shown before its turn commits: the sent text, then the stored message. */
export type PendingInput = Pick<StoryMessageRecord, 'raw'> &
  Partial<Pick<StoryMessageRecord, 'id' | 'display'>>;

/** `POST /sessions/{id}/preview`: the compiled next request, its token estimate and usage. */
export type StoryContextPreview = JsonObject & {
  trace: JsonObject[];
  lore: JsonObject[];
  estimated_tokens: number | null;
  budget_sources?: Record<string, number> | null;
  token_counting?: { tokenizer: string; source: string };
  compaction_opaque?: boolean;
  /** The planned cache breakpoint of this request. */
  cache?: { scope: string; revision: number; message_id: string | null };
  usage: JsonObject | null;
  cache_usage: ConversationCacheUsage | null;
};
