import { describe, expect, it } from 'vitest';
import { blankChannelProfile, channelProviders, profileKey, providerChannel } from '../../web/lib/channels';
import type { ModelProfile, Provider } from '../../web/lib/settingsDocument';

const providers: Provider[] = [
  {
    id: 'api',
    name: 'OpenAI API Key',
    base_url: 'https://api.openai.com',
    use_proxy: false,
    auth_mode: 'api_key',
  },
  {
    id: 'official',
    name: 'ChatGPT',
    base_url: 'https://api.openai.com',
    use_proxy: false,
    auth_mode: 'chatgpt_subscription',
  },
];
describe('channel origin', () => {
  it('uses authentication family, never the provider name or OpenAI domain', () => {
    expect(providerChannel(providers[0])).toBe('standard');
    expect(channelProviders(providers, 'chatgpt').map((p) => p.id)).toEqual(['official']);
    expect(channelProviders(providers, 'standard').map((p) => p.id)).toEqual(['api']);
  });
  it('keeps official Writer and Tracker profiles separate from ordinary profiles', () => {
    expect(profileKey('foreground', 'chatgpt')).toBe('official_foreground');
    expect(profileKey('background', 'chatgpt')).toBe('official_background');
    expect(profileKey('full_agent', 'standard')).toBe('model');
    expect(profileKey('card_editor', 'chatgpt')).toBe('card_editor');
  });
  it('does not copy a model or capabilities across channels', () => {
    const base = {
      provider_id: 'api',
      model: 'vendor-model',
      capability: 'accepted',
      capability_key: 'old',
      cache: { mode: 'runtime', request: { custom: true } },
    } as unknown as ModelProfile;
    const result = blankChannelProfile(base, providers, 'chatgpt');
    expect(result.provider_id).toBe('official');
    expect(result.model).toBe('');
    expect(result.capability).toBe('unknown');
    expect(result.api_mode).toBe('responses');
    expect(base.model).toBe('vendor-model');
    expect(blankChannelProfile(base, providers.slice(0, 1), 'chatgpt').provider_id).toBe('');
  });
});
