import type { CacheSettings } from './cachePolicy';
import type { SendShortcut } from './composerShortcut';
import type { MessageAdapterConfig } from './messageAdapter';
import type { KeminiSettings } from './kemini';
import type { NovelConfig } from './novel';
export type SettingsScope = 'story' | 'shared';
export type SettingsObject = Record<string, any>;
export interface ModelConnection {
  model: string;
  api_mode: string;
  api_path: string;
}
export interface ModelProfile extends ModelConnection {
  provider_id: string;
  provider_connections: Record<string, ModelConnection>;
  context_tokens: number;
  reply_tokens: number;
  reasoning_effort: string;
  tokenizer: string;
  temperature: number | null;
  top_p: number | null;
  top_k: number | null;
  frequency_penalty: number | null;
  presence_penalty: number | null;
  cache: CacheSettings;
  message_adapter?: MessageAdapterConfig;
  reasoning_format: string;
  capability: string;
  capability_key: string;
}
export type ConversationModelSelection = Pick<ModelProfile, 'provider_id' | 'model'> & {
  provider_models?: Record<string, string>;
};
export interface Provider {
  id: string;
  name: string;
  base_url: string;
  api_key?: string;
  has_api_key?: boolean;
  use_proxy: boolean;
  auth_mode?: 'api_key' | 'chatgpt_subscription' | 'openai_api_key';
  chatgpt_account_id?: string | null;
}
export interface PreferencePreset {
  id: string;
  name: string;
  content: string;
}
export type WebMode = 'off' | 'auto' | 'local';
export type WebService = 'exa' | 'tavily' | 'searxng' | 'duckduckgo';
export interface WebKeyService {
  enabled: boolean;
  auth_mode: 'keyless' | 'api_key';
  api_key?: string;
  has_api_key?: boolean;
}
export interface WebSettings {
  order: WebService[];
  exa: WebKeyService;
  tavily: WebKeyService;
  searxng: { enabled: boolean; endpoint: string };
  duckduckgo: { enabled: boolean };
  timeout_seconds: number;
}
export interface CardAuthorConfig {
  system_prompt: PromptChoices;
  max_steps: number;
  pressure_ratio: number;
  handoff_tokens: number;
  web: { mode: WebMode };
}
export interface SettingsPage {
  id: string;
  scope: SettingsScope;
  key: string;
  label: string;
  paths: string[];
}
export interface PromptDefinition {
  scope: SettingsScope;
  path: string;
  label: string;
  description: string;
  default: string;
  choices?: boolean;
}
export interface PromptChoice {
  text: string;
  name: string;
}
export interface PromptChoices {
  active: number;
  options: [PromptChoice, PromptChoice, PromptChoice, PromptChoice];
}
export interface SettingsDocument {
  version: 27;
  story: SettingsObject & {
    system_prompt: PromptChoices;
    novel_research: NovelConfig;
    media: import('./media').MediaDefaults;
    model: ModelProfile;
    official_model: ModelProfile | null;
    default_channel: 'standard' | 'chatgpt';
  };
  shared: SettingsObject & {
    kemini: KeminiSettings;
    unicode_story_enabled: boolean;
    providers: Provider[];
    card_author: ModelProfile;
    card_author_config: CardAuthorConfig;
    web: WebSettings;
    preference_presets: PreferencePreset[];
    send_shortcut: SendShortcut;
    debug_failed_requests: boolean;
    debug_story_prefix: boolean;
    advisor: {
      model: ModelProfile;
      official_model: ModelProfile | null;
      system_prompt: PromptChoices;
      recent_exchanges: number;
      keep_recent: number;
      auto_compact: boolean;
      context_strategy: 'local' | 'openai_compact';
      max_steps: number;
      web: { mode: WebMode };
    };
  };
  revisions: Record<SettingsScope, number>;
  pages: SettingsPage[];
  prompt_definitions: PromptDefinition[];
}
export const scopes: SettingsScope[] = ['story', 'shared'];
export interface SettingsUpdate {
  revisions: SettingsDocument['revisions'];
  changes: Partial<Record<SettingsScope, SettingsObject>>;
}
export const clone = <T>(value: T): T =>
  value === null || typeof value !== 'object' ? value : JSON.parse(JSON.stringify(value));

/** Settings are JSON. Compare large prompt strings and unchanged subtrees without serializing them. */
export function equal(a: any, b: any): boolean {
  if (a === b) return true;
  if (!a || !b || typeof a !== 'object' || typeof b !== 'object') return false;
  if (Array.isArray(a) || Array.isArray(b))
    return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => equal(v, b[i]));
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every((k) => Object.hasOwn(b, k) && equal(a[k], b[k]));
}
export function getPath(value: SettingsObject, path: string): any {
  return path.split('.').reduce((owner, key) => owner?.[key], value);
}
export function setPath(value: SettingsObject, path: string, replacement: any) {
  const parts = path.split('.');
  const key = parts.pop()!;
  const owner = parts.reduce((item, part) => item[part], value);
  owner[key] = clone(replacement);
}
/** Copy the ancestors of changed paths. All other saved fields stay immutable and shared. */
export function withFields<T extends SettingsObject>(base: T, changes: SettingsObject): T {
  const result = { ...base };
  for (const [path, value] of Object.entries(changes)) {
    const parts = path.split('.');
    let owner: SettingsObject = result,
      original: SettingsObject = base;
    for (const part of parts.slice(0, -1)) {
      if (owner[part] === original[part])
        owner[part] = Array.isArray(owner[part]) ? owner[part].slice() : { ...owner[part] };
      owner = owner[part];
      original = original[part];
    }
    owner[parts[parts.length - 1]] = value;
  }
  return result;
}

export function applySettingsUpdate(base: SettingsDocument, update: SettingsUpdate): SettingsDocument {
  if (scopes.every((scope) => base.revisions[scope] === update.revisions[scope])) return base;
  return {
    ...base,
    revisions: update.revisions,
    story: update.changes.story ? withFields(base.story, update.changes.story) : base.story,
    shared: update.changes.shared ? withFields(base.shared, update.changes.shared) : base.shared,
  };
}

/** Overlapping readers can finish out of order; revisions advance independently per scope. */
export function latestSettings(
  current: SettingsDocument | null | undefined,
  incoming: SettingsDocument,
): SettingsDocument {
  if (!current) return incoming;
  const story = incoming.revisions.story > current.revisions.story;
  const shared = incoming.revisions.shared > current.revisions.shared;
  if (!story && !shared) return current;
  if (story && shared) return incoming;
  const scope = story ? 'story' : 'shared';
  return {
    ...current,
    [scope]: incoming[scope],
    revisions: { ...current.revisions, [scope]: incoming.revisions[scope] },
  };
}

function nestedField(prefix: string, key: string, before: any, after: any): boolean {
  // These fields can replace protocol shapes or contain dotted provider IDs.
  return (
    before !== null &&
    after !== null &&
    typeof before === 'object' &&
    typeof after === 'object' &&
    !Array.isArray(before) &&
    !Array.isArray(after) &&
    !(prefix.endsWith('cache') && (key === 'value' || key === 'request')) &&
    key !== 'message_adapter' &&
    prefix !== 'media' &&
    key !== 'provider_connections'
  );
}

/** Arrays and newly created objects are atomic; existing object edits use leaf paths. */
export function difference(before: SettingsObject, after: SettingsObject, prefix = ''): SettingsObject {
  const result: SettingsObject = {};
  for (const [key, value] of Object.entries(after)) {
    const old = before[key];
    if (old === value) continue;
    const path = prefix ? `${prefix}.${key}` : key;
    if (nestedField(prefix, key, old, value)) Object.assign(result, difference(old, value, path));
    else if (!equal(old, value)) result[path] = clone(value);
  }
  return result;
}

function mergeDraft<T extends SettingsObject>(
  expected: SettingsObject,
  local: T,
  remote: SettingsObject,
  prefix = '',
): T {
  let result: SettingsObject = local;
  for (const key of new Set([...Object.keys(expected), ...Object.keys(remote)])) {
    const before = expected[key],
      current = local[key],
      saved = remote[key];
    if (before === saved || current === saved) continue;
    // Preserve edits made during this request (including a second secret).
    // Acknowledged secret fields disappear because the public value omits them.
    let value;
    if (
      nestedField(prefix, key, before, current) &&
      saved &&
      typeof saved === 'object' &&
      !Array.isArray(saved)
    ) {
      value = mergeDraft(before, current, saved, prefix ? `${prefix}.${key}` : key);
    } else {
      if (!equal(current, before) || equal(current, saved)) continue;
      value = clone(saved);
    }
    if (value === current) continue;
    if (result === local) result = { ...local };
    if (Object.hasOwn(remote, key)) result[key] = value;
    else delete result[key];
  }
  return result as T;
}

/** Only replaced server values are copied into the mutable draft. */
export function rebaseSettings(
  saved: SettingsDocument,
  draft: SettingsDocument,
  response: SettingsDocument,
  scope: SettingsScope,
  sent: SettingsObject,
): SettingsDocument {
  return {
    ...response,
    story: mergeDraft(
      scope === 'story' ? withFields(saved.story, sent) : saved.story,
      draft.story,
      response.story,
    ),
    shared: mergeDraft(
      scope === 'shared' ? withFields(saved.shared, sent) : saved.shared,
      draft.shared,
      response.shared,
    ),
  };
}
