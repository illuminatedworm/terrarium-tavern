import type { ModelProfile, Provider } from './settingsDocument';

export type Channel = 'standard' | 'chatgpt';
export const channelLabels: Record<Channel, string> = { standard: '普通 API', chatgpt: 'ChatGPT 官方' };
export const providerChannel = (provider?: Provider): Channel =>
  provider?.auth_mode === 'chatgpt_subscription' ? 'chatgpt' : 'standard';
export const channelProviders = (providers: Provider[], channel: Channel) =>
  providers.filter((provider) => providerChannel(provider) === channel);
export const profileKey = (role: string, channel: Channel) =>
  (channel === 'chatgpt' && role !== 'card_author' ? 'official_' : '') +
  (role === 'full_agent' ? 'model' : role);
export function blankChannelProfile(
  base: ModelProfile,
  providers: Provider[],
  channel: Channel,
): ModelProfile {
  return {
    ...JSON.parse(JSON.stringify(base)),
    provider_id: channelProviders(providers, channel)[0]?.id || '',
    model: '',
    capability: 'unknown',
    capability_key: '',
    ...(channel === 'chatgpt'
      ? { api_mode: 'responses', api_path: 'v1', cache: { ...base.cache, mode: 'cloud' } }
      : {}),
  };
}
