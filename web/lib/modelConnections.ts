import {
  clone,
  type ConversationModelSelection,
  type ModelConnection,
  type ModelProfile,
  type Provider,
} from './settingsDocument';
import { providerChannel } from './channels';

function connection(profile: ModelConnection): ModelConnection {
  return { api_mode: profile.api_mode, api_path: profile.api_path, model: profile.model };
}

/** Swap the visible connection with the selected provider's inactive record. */
export function selectProfileProvider(
  profile: ModelProfile,
  providerId: string,
  official = false,
): ModelProfile {
  const result = clone(profile);
  result.provider_connections = { ...result.provider_connections };
  if (providerId === profile.provider_id)
    return official ? { ...result, api_mode: 'responses', api_path: 'v1' } : result;
  const saved = result.provider_connections;
  const next = Object.hasOwn(saved, providerId)
    ? saved[providerId]
    : {
        api_mode: official ? 'responses' : 'chat_completions',
        api_path: official ? 'v1' : '',
        model: '',
      };
  delete saved[providerId];
  return {
    ...result,
    ...next,
    ...(official ? { api_mode: 'responses' as const, api_path: 'v1' } : {}),
    provider_id: providerId,
    provider_connections: {
      ...saved,
      ...(profile.provider_id ? { [profile.provider_id]: connection(profile) } : {}),
    },
  };
}

/** Conversation models override names only; protocol/path follow this role's provider record. */
export function conversationModelProfile(
  profile: ModelProfile,
  selection?: ConversationModelSelection | { provider_id?: undefined } | null,
  providers: Provider[] = [],
): ModelProfile {
  if (!selection?.provider_id) return clone(profile);
  const official = (id: string) => providerChannel(providers.find((p) => p.id === id)) === 'chatgpt';
  const result = selectProfileProvider(profile, selection.provider_id, official(selection.provider_id));
  result.model = selection.model;
  for (const [id, model] of Object.entries(selection.provider_models || {})) {
    if (id !== result.provider_id) {
      result.provider_connections = {
        ...result.provider_connections,
        [id]: { ...connection(selectProfileProvider(profile, id, official(id))), model },
      };
    }
  }
  return result;
}

export function modelConnectionSummary(profile: ModelProfile, provider?: Provider): string {
  const official = providerChannel(provider) === 'chatgpt';
  const mode = official ? 'responses' : profile.api_mode;
  const labels: Record<string, string> = {
    chat_completions: 'Chat Completions',
    responses: 'Responses',
    anthropic: 'Anthropic Messages',
    gemini: 'Google GenAI · Gemini',
  };
  const path = official ? 'v1' : profile.api_path || (mode === 'gemini' ? 'v1beta' : 'v1');
  const billing =
    provider?.auth_mode === 'openai_api_key'
      ? 'OpenAI 官方 API · API 计费 · '
      : provider?.auth_mode === 'chatgpt_subscription'
        ? 'SIWC · 订阅计费 · '
        : '';
  return `${billing}${labels[mode]} · /${path}。${official ? '官方连接固定使用此协议和路径；切换连接会重建云端压缩状态，可能增加输入量。' : '连接参数来自该角色的模型设置，可在设置中选择此提供商编辑；'}模型选择保存在当前对话。`;
}
