export interface AdapterRule {
  role: string;
  wrap: boolean;
}
export interface MessageAdapterConfig {
  version: 1;
  mappings: Record<string, Record<string, AdapterRule>>;
}
export interface MessageTypeDefinition {
  id: string;
  label: string;
  tag: string;
  configurable: boolean;
  appendable: boolean;
  defaults: Record<string, AdapterRule>;
}
export interface AdapterSettings {
  revision: number;
  config: Record<string, MessageAdapterConfig>;
  lanes: Array<{ id: string; label: string; api_mode: string; types: MessageTypeDefinition[] }>;
}
export const adapterApis = [
  { value: 'chat_completions', label: 'Chat Completions' },
  { value: 'responses', label: 'Responses' },
  { value: 'anthropic', label: 'Anthropic Messages' },
  { value: 'gemini', label: 'Gemini GenerateContent' },
];
export function adapterRule(
  config: MessageAdapterConfig | undefined,
  api: string,
  type: MessageTypeDefinition,
): AdapterRule {
  return config?.mappings?.[api]?.[type.id] || type.defaults[api];
}
export function updateAdapterRule(
  config: MessageAdapterConfig | undefined,
  api: string,
  type: MessageTypeDefinition,
  patch: Partial<AdapterRule>,
): MessageAdapterConfig {
  const next: MessageAdapterConfig = JSON.parse(JSON.stringify(config || { version: 1, mappings: {} }));
  next.mappings[api] ||= {};
  next.mappings[api][type.id] = { ...adapterRule(config, api, type), ...patch };
  return next;
}
