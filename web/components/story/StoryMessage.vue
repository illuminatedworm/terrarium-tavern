<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import StoryProse from './StoryProse.vue';
import { marked } from 'marked';
import { loadStoryRuntime } from '../../lib/storyRuntime';
const props = defineProps<{
  message: any;
  session: any;
  enabled: boolean;
  hidePanel?: boolean;
  expandFirstLevel?: boolean;
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
const frame = ref<HTMLIFrameElement>();
const source = ref('');
const height = ref(280);
let token = crypto.randomUUID();
let buildVersion = 0;
const raw = computed(() =>
  (props.message.display || props.message.raw).replace(
    /\{\{user\}\}/g,
    () => props.session.persona || '旅人',
  ),
);
const htmlCard = computed(() => {
  const tokens = marked.lexer(raw.value);
  const isInterface = (text: string) => /<(?:html|!doctype|script|style|div|section)\b/i.test(text);
  for (const token of tokens) {
    if (
      token.type === 'code' &&
      (!token.lang || token.lang.toLowerCase() === 'html') &&
      isInterface(token.text)
    )
      return token.text;
  }
  return tokens.some((token) => token.type === 'html' && isInterface(token.text)) ? raw.value : '';
});
const hiddenPanel = computed(
  () => props.hidePanel && /stat_data|getAllVariables|getMvuData|StatusPlaceHolder/i.test(htmlCard.value),
);
const narrativeOnly = computed(() => {
  const text = raw.value.split(/```(?:html)?|<(?:html|!doctype|style|div|section|script)\b/i)[0].trim();
  return text || '场景面板与变量显示在右侧栏。';
});
async function build() {
  const version = ++buildVersion;
  if (!htmlCard.value || !props.enabled || hiddenPanel.value) {
    source.value = '';
    return;
  }
  token = crypto.randomUUID();
  const [sdk, core] = await loadStoryRuntime();
  if (version !== buildVersion) return;
  const data = {
    ui: true,
    currentMessage: props.session.messages.findIndex((m: any) => m.id === props.message.id),
    state: props.message.snapshot.state,
    scopes: props.message.snapshot.scopes,
    messages: props.session.messages.map((m: any, i: number) => ({
      message_id: i,
      id: m.id,
      role: m.role,
      message: m.raw,
      variables: m.snapshot.state,
    })),
  };
  const escape = (text: string) => text.replace(/<\/script/gi, '<\\/script');
  const bootstrap = `const token=${JSON.stringify(token)};const pending=new Map();
    const host=(method,args)=>new Promise((resolve,reject)=>{const id=crypto.randomUUID();pending.set(id,{resolve,reject});parent.postMessage({token,id,method,args},'*');});
    addEventListener('message',e=>{if(e.source!==parent||e.data.token!==token)return;const p=pending.get(e.data.id);if(p){if(e.data.stream!==undefined){p.full=(p.full||'')+e.data.stream;TavernRuntime.emit('stream_token_received_incrementally',e.data.stream);TavernRuntime.emit('stream_token_received_fully',p.full);return;}pending.delete(e.data.id);e.data.error?p.reject(Error(e.data.error)):p.resolve(e.data.result);}});
    TavernRuntime.install(${JSON.stringify(data)},host);
    addEventListener('DOMContentLoaded',()=>{if(!document.querySelector('#send_textarea')){const proxy=document.createElement('textarea');proxy.id='send_textarea';proxy.hidden=true;proxy.addEventListener('input',()=>host('input',{text:proxy.value}));document.body.append(proxy);}});
    if (${JSON.stringify(!!props.expandFirstLevel)}) {
      const initialized = new WeakSet();
      const expand = () => document.querySelectorAll('details, .monitor-container').forEach(el => {
        if (!initialized.has(el)) {
          initialized.add(el);
          if (!el.parentElement?.closest('details, .monitor-container')) {
            if (el.tagName === 'DETAILS') el.open = true;
            else el.classList.add('expanded');
          }
        }
      });
      addEventListener('DOMContentLoaded', () => {
        expand();
        new MutationObserver(expand).observe(document.body, {childList:true, subtree:true});
      });
    }
    addEventListener('load',()=>new ResizeObserver(()=>parent.postMessage({token,method:'height',args:document.body.scrollHeight},'*')).observe(document.body));
    addEventListener('error',e=>host('log',{message:e.message}));`;
  source.value = `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; style-src 'unsafe-inline' data:; img-src data: blob:; font-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'"><style>html{background:#222326;scrollbar-width:thin;scrollbar-color:#50515c transparent}body{margin:0;color:#b9b7be;font:15px system-ui}button{cursor:pointer}*{box-sizing:border-box}::-webkit-scrollbar{width:8px;height:8px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{border:2px solid transparent;border-radius:999px;background:#50515c;background-clip:content-box}</style><script>${escape(sdk)}<\/script><script>${escape(core)}<\/script><script>${escape(bootstrap)}<\/script></head><body>${htmlCard.value}</body></html>`;
}
function receive(event: MessageEvent) {
  if (event.source !== frame.value?.contentWindow || event.data?.token !== token) return;
  const { method, args, id } = event.data;
  if (method === 'height') {
    height.value = Math.min(1500, Math.max(100, Number(args) || 280));
    return;
  }
  if (!['generate', 'variables', 'messages', 'lore', 'cancel', 'log', 'input'].includes(method)) return;
  const reply = (result: any, error?: string) =>
    frame.value?.contentWindow?.postMessage({ token, id, result, error }, '*');
  emit(
    'action',
    method,
    args,
    (result) => reply(result),
    (error) => reply(null, String(error)),
    (stream) => frame.value?.contentWindow?.postMessage({ token, id, stream }, '*'),
  );
}
window.addEventListener('message', receive);
onBeforeUnmount(() => {
  ++buildVersion;
  window.removeEventListener('message', receive);
});
watch(
  () => [
    props.session.id,
    props.message.id,
    props.message.display,
    props.message.raw,
    props.message.snapshot,
    props.enabled,
    props.hidePanel,
    props.expandFirstLevel,
  ],
  build,
  { immediate: true, deep: true },
);
</script>
<template>
  <StoryProse v-if="hiddenPanel" :text="narrativeOnly" />
  <iframe
    v-else-if="source"
    ref="frame"
    class="card-frame"
    sandbox="allow-scripts"
    :srcdoc="source"
    :style="{ height: height + 'px' }"
    title="角色卡交互界面"
    referrerpolicy="no-referrer"
  />
  <StoryProse v-else :text="raw" :breaks="message.role === 'user'" />
  <p v-if="htmlCard && !enabled" class="muted">此消息包含交互界面。可从左侧“角色卡”管理中启用脚本。</p>
</template>
