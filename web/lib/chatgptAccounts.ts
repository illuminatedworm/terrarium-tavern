import { reactive, ref } from 'vue';
import { api } from './api';
import type { ModelProfile, Provider } from './settingsDocument';

export const CHATGPT_USAGE_URL = 'https://chatgpt.com/settings/usage';
export interface ChatGPTAccount {
  id: string;
  label: string;
  email: string;
  status: 'connected' | 'permission_required' | 'signed_out';
  plan_enabled: boolean;
  generation: string;
}
export interface ChatGPTLogin {
  id?: string;
  provider_id?: string;
  account_id?: string;
  status: 'idle' | 'pending' | 'exchanging' | 'completed' | 'cancelled' | 'expired' | 'failed';
  error?: string;
}
export const chatgptState = reactive({ accounts: [] as ChatGPTAccount[], welcome_required: false });
export const chatgptLogin = ref<ChatGPTLogin>({ status: 'idle' });
export const completedLogins = new Set<string>();
let fetching: Promise<void> | undefined;

export function loadChatGPTAccounts() {
  if (fetching) return fetching;
  fetching = api('/auth/chatgpt/accounts')
    .then((value) => {
      chatgptState.accounts = value.accounts;
      chatgptState.welcome_required = value.welcome_required;
    })
    .finally(() => {
      fetching = undefined;
    });
  return fetching;
}

export const subscriptionProvider = (provider?: Provider) => provider?.auth_mode === 'chatgpt_subscription';
export function usesChatGPT(profile: ModelProfile | undefined, providers: Provider[] = []) {
  return subscriptionProvider(providers.find((provider) => provider.id === profile?.provider_id));
}
export function chatgptGeneration(provider?: Provider) {
  return (
    chatgptState.accounts.find((account) => account.id === provider?.chatgpt_account_id)?.generation || ''
  );
}
export function loginPending(providerId?: string) {
  return (
    ['pending', 'exchanging'].includes(chatgptLogin.value.status) &&
    (!providerId || chatgptLogin.value.provider_id === providerId)
  );
}
