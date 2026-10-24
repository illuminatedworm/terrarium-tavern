<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { loadStoryRuntime } from '../../lib/storyRuntime';
import {
  acquireHistory,
  acquireMarkup,
  reservePanelState,
  scriptAsset,
  type AssetLease,
} from '../../lib/storyAssets';
import { frameCapabilities } from '../../../compat/frame-capabilities';
import { frameBoot } from '../../lib/storyFrameBoot';
import { api } from '../../lib/api';
import { isolatedCardScripts } from '../../lib/hostCapabilities';
const props = defineProps<{
  message: any;
  session: any;
  enabled: boolean;
  html: string;
  character: string;
  expandFirstLevel?: boolean;
  findInDialogue?: boolean;
}>();
const emit = defineEmits<{
  action: [
    method: string,
    args: any,
    resolve: (value: any) => void,
    reject: (error: any) => void,
    stream: (chunk: string) => void,
  ];
}>();
const shell = ref<HTMLElement>();
const frame = ref<HTMLIFrameElement>();
const source = ref('');
const loadError = ref('');
const height = ref(280);
const suspended = ref(false);
const needsScripts = computed(() => /<script\b|\bon[a-z]+\s*=|\bjavascript\s*:/i.test(props.html));
const capabilities = computed(() => frameCapabilities(props.html));
let token = crypto.randomUUID();
let buildVersion = 0;
let updateVersion = 0;
let ready = false;
let visible = true;
let disposed = false;
let liveFailed = false;
let builtKey: unknown[] | undefined;
let assets: { release: () => void }[] = [];
let retired: { release: () => void }[] = [];
let historyAsset: AssetLease | undefined;
let oldHistories: AssetLease[] = [];
let saved: ReturnType<typeof reservePanelState>;
let observer: IntersectionObserver | undefined;
let suspensionTimer: ReturnType<typeof setTimeout> | undefined;
let boot: { html: string; assets: { url: string; blob: Blob }[] } | undefined;
let imageRequests = new AbortController();
const release = (items: { release: () => void }[]) => items.splice(0).forEach((item) => item.release());
// Card frames are isolated documents and cannot read host variables, so the host
// tokens are resolved once and written into the frame's fallback stylesheet.
function baseStyle() {
  const tokens = getComputedStyle(document.documentElement);
  const token = (name: string) => tokens.getPropertyValue(name).trim();
  return `html{background:${token('--surface-panel')};scrollbar-width:thin;scrollbar-color:${token('--scrollbar')} transparent}body{margin:0;color:${token('--text-secondary')};font:15px system-ui}button{cursor:pointer}*{box-sizing:border-box}`;
}
function data() {
  return {
    ui: true,
    persona: props.session.persona,
    character: props.character,
    currentMessage: props.session.messages.findIndex((m: any) => m.id === props.message.id),
    state: props.message.snapshot.state,
    scopes: props.message.snapshot.scopes,
  };
}
function key() {
  return [
    props.session.id,
    capabilities.value.liveState ? '' : props.message.id,
    props.html,
    props.enabled,
    props.character,
    props.expandFirstLevel,
    props.findInDialogue,
  ];
}
function clearAssets() {
  imageRequests.abort();
  imageRequests = new AbortController();
  boot = undefined;
  release(assets);
  release(retired);
  release(oldHistories);
  historyAsset?.release();
  historyAsset = undefined;
}
function releaseRetired() {
  release(retired);
  release(oldHistories);
}
function requestSuspend() {
  clearTimeout(suspensionTimer);
  if (!visible && ready && capabilities.value.suspend && document.activeElement !== frame.value)
    suspensionTimer = setTimeout(() => {
      if (!visible && ready && document.activeElement !== frame.value)
        frame.value?.contentWindow?.postMessage({ token, method: 'suspend', version: updateVersion }, '*');
    }, 250);
}
async function build(force = false) {
  if (builtKey && builtKey[0] !== props.session.id) {
    // Revoke the previous session's bridge immediately, even while a download is pending.
    token = crypto.randomUUID();
    ready = false;
    source.value = '';
    clearAssets();
    saved?.release();
    saved = undefined;
    suspended.value = false;
    builtKey = undefined;
  }
  if (disposed || (suspended.value && !force)) return;
  const version = ++buildVersion;
  loadError.value = '';
  if (!props.html || (needsScripts.value && (!props.enabled || !isolatedCardScripts))) {
    token = crypto.randomUUID();
    source.value = '';
    ready = false;
    clearAssets();
    return;
  }
  const nextKey = key();
  const sameKey = builtKey?.every((value, index) => value === nextKey[index]);
  if (!force && source.value && sameKey && !needsScripts.value) return;
  if (
    !force &&
    ready &&
    source.value &&
    sameKey &&
    capabilities.value.liveState &&
    !liveFailed &&
    needsScripts.value
  ) {
    const next = acquireHistory(props.session.messages);
    if (historyAsset) oldHistories.push(historyAsset);
    historyAsset = next;
    frame.value?.contentWindow?.postMessage(
      {
        token,
        method: 'state',
        data: JSON.parse(JSON.stringify(data())),
        history: next.blob,
        version: ++updateVersion,
      },
      '*',
    );
    return;
  }
  let sdk: string | undefined;
  let html = props.html;
  if (/https:\/\//i.test(html)) {
    try {
      const prepared = await api(
        `/sessions/${props.session.id}/images/markup`,
        {
          html,
          message_id: props.message.id,
        },
        'POST',
        imageRequests.signal,
      );
      html = prepared.html;
    } catch (error) {
      if (version === buildVersion) loadError.value = String(error);
      return;
    }
  }
  if (needsScripts.value) {
    try {
      sdk = await loadStoryRuntime();
    } catch (error) {
      if (version === buildVersion) loadError.value = String(error);
      return;
    }
  }
  if (version !== buildVersion || disposed) return;
  if (builtKey && !sameKey) {
    saved?.release();
    saved = undefined;
    liveFailed = false;
  }
  builtKey = nextKey;
  retired.push(...assets);
  assets = [];
  if (historyAsset) {
    oldHistories.push(historyAsset);
    historyAsset = undefined;
  }
  token = crypto.randomUUID();
  imageRequests.abort();
  imageRequests = new AbortController();
  ready = false;
  ++updateVersion;
  // Shared assets are opt-in for scripts which might inspect their original DOM/CSSOM.
  const markup =
    needsScripts.value && capabilities.value.sharedAssets
      ? acquireMarkup(html)
      : { html, leases: [], release() {} };
  assets.push(markup);
  let scripts = '';
  const transfers: AssetLease[] = [...markup.leases];
  if (sdk) {
    const sdkAsset = scriptAsset(sdk);
    assets.push(sdkAsset);
    historyAsset = acquireHistory(props.session.messages);
    transfers.push(sdkAsset, historyAsset);
    const options = JSON.stringify({
      token,
      data: data(),
      expandFirstLevel: props.expandFirstLevel,
      findInDialogue: props.findInDialogue,
      hasSaved: !!saved,
      saved: saved?.value,
    }).replace(/<\/script/gi, '<\\/script');
    scripts = `<script src="${sdkAsset.url}"><\/script><script src="${historyAsset.url}"><\/script><script>TavernRuntime.mountFrame(TavernRuntime,${options});<\/script>`;
  }
  const policy = `default-src 'none'; script-src ${sdk ? "'unsafe-inline' 'unsafe-eval' blob:" : "'none'"}; style-src 'unsafe-inline' data: blob:; img-src data: blob:; font-src data: blob:; connect-src 'none'; form-action 'none'; base-uri 'none'`;
  const documentHtml = `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${policy}"><style>${baseStyle()}</style>${scripts}</head><body>${markup.html}</body></html>`;
  boot = sdk ? { html: documentHtml, assets: transfers.map(({ url, blob }) => ({ url, blob })) } : undefined;
  source.value = sdk ? frameBoot(token) : documentHtml;
}
function receive(event: MessageEvent) {
  if (event.source !== frame.value?.contentWindow || event.data?.token !== token) return;
  const { method, args, id } = event.data;
  if (method === 'boot') {
    if (boot) frame.value?.contentWindow?.postMessage({ token, method: 'boot', ...boot }, '*');
    boot = undefined;
    return;
  }
  if (method === 'ready') {
    ready = true;
    saved?.release();
    saved = undefined;
    releaseRetired();
    requestSuspend();
    return;
  }
  if (method === 'updated') {
    if (args === updateVersion) {
      release(oldHistories);
      requestSuspend();
    }
    return;
  }
  if (method === 'saved') {
    if (visible || event.data.version !== updateVersion || document.activeElement === frame.value) return;
    const reservation = reservePanelState(args);
    if (!reservation) return;
    saved?.release();
    saved = reservation;
    suspended.value = true;
    ++buildVersion;
    ready = false;
    source.value = '';
    clearAssets();
    return;
  }
  if (method === 'state-error') {
    liveFailed = true;
    void build(true);
    return;
  }
  if (method === 'restore-error') {
    loadError.value = `面板恢复失败：${args}`;
    return;
  }
  if (method === 'image-error') {
    loadError.value = `图片加载失败：${args}`;
    return;
  }
  if (method === 'find') {
    if (frame.value?.closest('.message')) window.dispatchEvent(new Event('tavern-dialogue-find'));
    return;
  }
  if (method === 'height') {
    height.value = Math.min(1500, Math.max(100, Number(args) || 280));
    return;
  }
  // Replies belong to the requesting document even if an async action finishes after navigation.
  const destination = frame.value?.contentWindow;
  const requestToken = token;
  const reply = (result: any, error?: string) =>
    destination?.postMessage({ token: requestToken, id, result, error }, '*');
  if (method === 'image') {
    if (!props.enabled) return reply(null, '此对话尚未启用脚本');
    void api(
      `/sessions/${props.session.id}/images`,
      {
        url: args?.url,
        message_id: props.message.id,
      },
      'POST',
      imageRequests.signal,
    )
      .then((result) => {
        if (!disposed && requestToken === token) reply(result.url);
      })
      .catch((error) => {
        if (!disposed && requestToken === token) reply(null, String(error));
      });
    return;
  }
  if (method === 'runtime-core') {
    void loadStoryRuntime('core')
      .then((core) => {
        if (disposed || requestToken !== token || suspended.value) return;
        const lease = scriptAsset(core);
        assets.push(lease);
        reply(lease.blob);
      })
      .catch((error) => reply(null, String(error)));
    return;
  }
  if (!['generate', 'variables', 'messages', 'lore', 'cancel', 'log', 'input'].includes(method)) return;
  emit(
    'action',
    method,
    args,
    (result) => reply(result),
    (error) => reply(null, String(error)),
    (stream) => destination?.postMessage({ token: requestToken, id, stream }, '*'),
  );
}
window.addEventListener('message', receive);
onMounted(() => {
  observer = new IntersectionObserver(
    ([entry]) => {
      visible = entry.isIntersecting;
      if (visible && suspended.value) {
        suspended.value = false;
        void build(true);
      } else requestSuspend();
    },
    { root: shell.value?.closest('.messages') || null, rootMargin: '600px' },
  );
  if (shell.value) observer.observe(shell.value);
});
onBeforeUnmount(() => {
  disposed = true;
  ++buildVersion;
  observer?.disconnect();
  clearTimeout(suspensionTimer);
  window.removeEventListener('message', receive);
  clearAssets();
  saved?.release();
});
watch(
  () => [
    props.session.id,
    props.message.id,
    props.message.raw,
    props.message.snapshot.state,
    props.message.snapshot.scopes,
    props.enabled,
    props.html,
    props.character,
    props.session.persona,
    props.expandFirstLevel,
    props.findInDialogue,
  ],
  () => void build(),
  { immediate: true, deep: true },
);
watch(
  () => props.session.messages,
  () => {
    if (capabilities.value.liveState) void build();
  },
);
</script>
<template>
  <div ref="shell" class="frame-shell" :style="suspended ? { height: height + 'px' } : undefined">
    <iframe
      v-if="source"
      ref="frame"
      class="card-frame"
      :sandbox="enabled && isolatedCardScripts && needsScripts ? 'allow-scripts' : ''"
      :srcdoc="source"
      :style="{ height: height + 'px' }"
      title="角色卡交互界面"
      referrerpolicy="no-referrer"
      @load="!needsScripts && releaseRetired()"
    />
    <p v-else-if="suspended" class="muted" role="status">面板将在滚动到此处时恢复。</p>
    <p v-else-if="!enabled" class="muted" role="status">
      此对话尚未启用脚本，交互面板已暂停。可在会话设置中启用。
    </p>
    <p v-else-if="needsScripts && !isolatedCardScripts" class="muted" role="status">
      当前窗口无法隔离卡片网络访问，交互面板已暂停。请从桌面启动入口打开应用窗口；Android 请更新系统 WebView。
    </p>
    <p v-else-if="!loadError" class="muted" role="status">正在加载交互面板…</p>
    <div v-if="loadError" class="error" role="alert">
      {{ loadError }} <button @click="build(true)">重新加载面板</button>
    </div>
  </div>
</template>
