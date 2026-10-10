<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { api } from '../../lib/api';
import {
  CHATGPT_USAGE_URL,
  chatgptLogin,
  chatgptState,
  completedLogins,
  loadChatGPTAccounts,
  loginPending,
} from '../../lib/chatgptAccounts';
import type { Provider } from '../../lib/settingsDocument';

const props = defineProps<{ provider: Provider; flush: () => Promise<unknown> }>();
const busy = ref(false),
  feedback = ref('');
const account = computed(() =>
  chatgptState.accounts.find((item) => item.id === props.provider.chatgpt_account_id),
);
const pending = computed(() => loginPending(props.provider.id));
let timer: ReturnType<typeof setTimeout> | undefined;
let alive = true;
let ownedAttempt = '';

async function poll() {
  clearTimeout(timer);
  try {
    chatgptLogin.value = await api('/auth/chatgpt/login');
    const attempt = chatgptLogin.value;
    if (pending.value) {
      ownedAttempt = attempt.id || '';
      if (alive) timer = setTimeout(poll, 1000);
    } else if (attempt.id === ownedAttempt && !completedLogins.has(ownedAttempt)) {
      completedLogins.add(ownedAttempt);
      if (attempt.status === 'completed') {
        await loadChatGPTAccounts();
        if (alive && attempt.account_id && attempt.provider_id === props.provider.id) {
          props.provider.chatgpt_account_id = attempt.account_id;
          await props.flush();
          feedback.value = '账号已连接，请在各 Agent 的模型设置中选择此提供商和模型。';
        }
      } else
        feedback.value =
          attempt.error || (attempt.status === 'expired' ? '登录已过期，请重试。' : '登录已取消。');
    }
  } catch (error) {
    feedback.value = String(error);
    if (alive && pending.value) timer = setTimeout(poll, 3000);
  }
}

async function login(existing = false) {
  busy.value = true;
  feedback.value = '';
  try {
    await props.flush();
    chatgptLogin.value = await api('/auth/chatgpt/login', {
      provider_id: props.provider.id,
      account_id: existing ? props.provider.chatgpt_account_id : null,
      enable_plan: existing && account.value?.status === 'permission_required',
    });
    ownedAttempt = chatgptLogin.value.id || '';
    if (alive) void poll();
  } catch (error) {
    feedback.value = String(error);
  } finally {
    busy.value = false;
  }
}
async function cancel() {
  busy.value = true;
  try {
    chatgptLogin.value = await api(`/auth/chatgpt/login/${chatgptLogin.value.id}`, undefined, 'DELETE');
    feedback.value = '登录已取消。';
    clearTimeout(timer);
  } catch (error) {
    feedback.value = String(error);
  } finally {
    busy.value = false;
  }
}
async function logout() {
  if (!account.value) return;
  busy.value = true;
  try {
    await props.flush();
    const result = await api(`/auth/chatgpt/accounts/${account.value.id}/logout`, {
      provider_id: props.provider.id,
    });
    feedback.value = result.message;
    await loadChatGPTAccounts();
  } catch (error) {
    feedback.value = String(error);
  } finally {
    busy.value = false;
  }
}
onMounted(async () => {
  try {
    await loadChatGPTAccounts();
    if (alive) await poll();
  } catch (error) {
    feedback.value = String(error);
  }
});
onBeforeUnmount(() => {
  alive = false;
  clearTimeout(timer);
});
</script>

<template>
  <div class="chatgpt-connection">
    <p class="muted">使用符合资格的 ChatGPT 付费账号订阅额度。登录与允许消耗额度是两项独立授权。</p>
    <label
      >ChatGPT 账号
      <select v-model="provider.chatgpt_account_id" :disabled="busy || pending" aria-label="ChatGPT 账号">
        <option :value="null">选择账号</option>
        <option v-if="provider.chatgpt_account_id && !account" :value="provider.chatgpt_account_id">
          此安装尚未连接，请重新添加账号
        </option>
        <option v-for="item in chatgptState.accounts" :key="item.id" :value="item.id">
          {{ item.label }} · {{ item.email || 'ChatGPT 账号'
          }}{{
            item.status === 'signed_out'
              ? '（已退出）'
              : item.status === 'permission_required'
                ? '（未授权订阅使用）'
                : ''
          }}
        </option>
      </select>
    </label>
    <p v-if="account" class="chatgpt-status" role="status">
      {{
        account.status === 'connected'
          ? '已连接 · 已启用 ChatGPT 订阅使用'
          : account.status === 'permission_required'
            ? '已登录 · 未启用订阅使用权限'
            : '已退出，请重新登录'
      }}
    </p>
    <div class="chatgpt-actions">
      <button class="chatgpt-signin" type="button" :disabled="busy || loginPending()" @click="login(false)">
        Continue with ChatGPT
      </button>
      <button v-if="account" type="button" :disabled="busy || loginPending()" @click="login(true)">
        {{ account.status === 'permission_required' ? '启用订阅使用' : '重新登录此账号' }}
      </button>
      <button
        v-if="account && account.status !== 'signed_out'"
        type="button"
        :disabled="busy || pending"
        @click="logout"
      >
        退出账号
      </button>
      <a :href="CHATGPT_USAGE_URL" target="_blank" rel="noopener noreferrer">管理用量</a>
    </div>
    <p v-if="pending" role="status" class="chatgpt-pending">
      {{
        chatgptLogin.status === 'exchanging' ? '正在验证登录…' : '请在系统浏览器中完成授权。十分钟内有效。'
      }}
      <button type="button" :disabled="busy" @click="cancel">取消登录</button>
    </p>
    <p v-else-if="loginPending()" class="muted">另一个提供商正在登录 ChatGPT，请先完成或取消。</p>
    <p v-if="feedback" role="status" class="inline-feedback">{{ feedback }}</p>
    <p class="muted">
      账号连接与退出立即生效，不随设置撤回。退出会停止此账号的运行中请求；订阅额度不足时不会自动切换计费方式。
    </p>
  </div>
</template>
