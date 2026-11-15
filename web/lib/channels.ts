import type { ModelProfile, Provider } from './settingsDocument';

export type Channel = 'standard' | 'chatgpt';
export const channelLabels: Record<Channel, string> = { standard: '普通 API', chatgpt: 'OpenAI 官方' };
export const providerChannel = (provider?: Provider): Channel =>
  provider?.auth_mode === 'chatgpt_subscription' || provider?.auth_mode === 'openai_api_key'
    ? 'chatgpt'
    : 'standard';
export const channelProviders = (providers: Provider[], channel: Channel) =>
  providers.filter((provider) => providerChannel(provider) === channel);
export const storyProfileKey = (channel: Channel) => (channel === 'chatgpt' ? 'official_model' : 'model');
export function blankChannelProfile(
  base: ModelProfile,
  providers: Provider[],
  channel: Channel,
): ModelProfile {
  return {
    ...JSON.parse(JSON.stringify(base)),
    provider_id: channelProviders(providers, channel)[0]?.id || '',
    model: '',
    provider_connections: {},
    capability: 'unknown',
    capability_key: '',
    ...(channel === 'chatgpt'
      ? { api_mode: 'responses', api_path: 'v1', cache: { ...base.cache, mode: 'cloud' } }
      : { api_mode: 'chat_completions', api_path: '' }),
  };
}
