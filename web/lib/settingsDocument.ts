import type { CacheSettings } from './cachePolicy';
import type { SendShortcut } from './composerShortcut';
import type { MessageAdapterConfig } from './messageAdapter';
import type { KeminiSettings } from './kemini';
export type SettingsScope = 'full_agent' | 'half_agent' | 'shared';
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
  auth_mode?: 'api_key' | 'chatgpt_subscription';
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
  system_prompt: string;
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
}
export interface SettingsDocument {
  version: 12;
  full_agent: SettingsObject & {
    model: ModelProfile;
    official_model: ModelProfile | null;
    default_channel: 'standard' | 'chatgpt';
  };
  half_agent: SettingsObject & {
    foreground: ModelProfile;
    background: ModelProfile;
    official_foreground: ModelProfile | null;
    official_background: ModelProfile | null;
    default_channel: 'standard' | 'chatgpt';
  };
  shared: SettingsObject & {
    kemini: KeminiSettings;
    providers: Provider[];
    card_author: ModelProfile;
    card_author_config: CardAuthorConfig;
    web: WebSettings;
    preference_presets: PreferencePreset[];
    send_shortcut: SendShortcut;
    debug_failed_requests: boolean;
    debug_full_agent_prefix: boolean;
    advisor: {
      model: ModelProfile;
      official_model: ModelProfile | null;
      system_prompt: string;
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
export const scopes: SettingsScope[] = ['full_agent', 'half_agent', 'shared'];
export const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value));
export const equal = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
export function getPath(value: SettingsObject, path: string): any {
  return path.split('.').reduce((owner, key) => owner?.[key], value);
}
export function setPath(value: SettingsObject, path: string, replacement: any) {
  const parts = path.split('.');
  const key = parts.pop()!;
  const owner = parts.reduce((item, part) => item[part], value);
  owner[key] = clone(replacement);
}
/** Arrays and newly created objects are atomic; existing object edits use leaf paths. */
export function difference(before: SettingsObject, after: SettingsObject, prefix = ''): SettingsObject {
  const result: SettingsObject = {};
  for (const key of Object.keys(after)) {
    const path = prefix ? `${prefix}.${key}` : key;
    const value = after[key];
    if (equal(before?.[key], value)) continue;
    if (
      value &&
      typeof value === 'object' &&
      !Array.isArray(value) &&
      // Nullable channel profiles must be created before their children can be patched or rebased.
      before?.[key] &&
      typeof before[key] === 'object' &&
      !Array.isArray(before[key]) &&
      !(prefix.endsWith('cache') && (key === 'value' || key === 'request')) &&
      key !== 'message_adapter' &&
      // Provider IDs can contain dots, and switching removes the active entry.
      key !== 'provider_connections'
    ) {
      Object.assign(result, difference(before[key], value, path));
    } else result[path] = clone(value);
  }
  return result;
}
/** Rebase only edits not acknowledged by this request, including edits made in flight. */
export function rebaseSettings(
  saved: SettingsDocument,
  draft: SettingsDocument,
  response: SettingsDocument,
  scope: SettingsScope,
  sent: SettingsObject,
): SettingsDocument {
  const expected = clone(saved);
  for (const [path, value] of Object.entries(sent)) setPath(expected[scope], path, value);
  const next = clone(response);
  for (const name of scopes) {
    for (const [path, value] of Object.entries(difference(expected[name], draft[name])))
      setPath(next[name], path, value);
  }
  return next;
}
