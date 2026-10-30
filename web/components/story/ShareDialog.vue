<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import Icon from '../common/Icon.vue';
import quoteFontUrl from '../../assets/quote-marks.ttf';
import {
  IMAGE_SCALE,
  IMAGE_WIDTH,
  shareCss,
  shareDocument,
  shareEntries,
  shareMessageHtml,
  sharePng,
  shareRangeLabel,
  shareRounds,
  shareText,
} from '../../lib/conversationShare';
import { saveDownload } from '../../lib/download';
import { androidHost } from '../../lib/hostCapabilities';
import { historySummary } from '../../lib/storyHistory';

const props = defineProps<{
  session: any;
  name: string;
  portraitSrc?: string;
  reading: Record<string, string | number>;
}>();

// Canvas height limits: Chromium allows 32767 px; Android WebViews often fail sooner.
const MAX_IMAGE_HEIGHT = androidHost ? 16384 : 32767;
const PALETTE = [
  'surface-base',
  'surface-hover',
  'border-strong',
  'text-primary',
  'text-secondary',
  'text-muted',
  'text-story',
  'text-dialogue',
  'accent-hover',
];

const rounds = computed(() => shareRounds(props.session.messages || []));
const first = computed(() => rounds.value[0]?.number ?? 1);
const last = computed(() => rounds.value.at(-1)?.number ?? 1);
const from = ref(first.value);
const to = ref(last.value);
const alias = ref(props.session.persona || '');
// Number inputs can hold blanks or out-of-range values while typing.
const clamp = (value: number) => Math.min(last.value, Math.max(first.value, Math.round(Number(value)) || 0));
watch(from, (value) => {
  if (clamp(value) !== value) from.value = clamp(value);
  else if (value > to.value) to.value = value;
});
watch(to, (value) => {
  if (clamp(value) !== value) to.value = clamp(value);
  else if (value < from.value) from.value = value;
});
function endpoint(number: number) {
  const round = rounds.value.find((item) => item.number === number);
  const user = round?.messages.find((message) => message.role === 'user');
  const reply = round?.messages.find((message) => message.role === 'assistant');
  return {
    user: user ? historySummary(user) : '故事开场',
    reply: historySummary(reply) || '尚无回复',
  };
}
const messageCount = computed(
  () =>
    rounds.value
      .filter((round) => round.number >= from.value && round.number <= to.value)
      .flatMap((round) => round.messages).length,
);
const persona = computed(() => String(props.session.persona || '').trim());

// One generated document per range and form of address; previews and both
// exports reuse it instead of fetching every image again.
const key = computed(() =>
  JSON.stringify([props.session.id, props.session.head, from.value, to.value, alias.value]),
);
const built = ref<{ key: string; html: string } | null>(null);
const current = computed(() => (built.value?.key === key.value ? built.value.html : ''));
const previewHeight = ref<{ key: string; height: number } | null>(null);
const imageHeight = computed(() =>
  previewHeight.value?.key === key.value ? previewHeight.value.height * IMAGE_SCALE : 0,
);
const showPreview = ref(false);
const imageSize = computed(
  () => `${(IMAGE_WIDTH * IMAGE_SCALE).toLocaleString()} × ${imageHeight.value.toLocaleString()}`,
);
const status = ref('');
const error = ref('');
const working = ref(false);
let controller = new AbortController();

// Narrow windows shrink the preview instead of scrolling it sideways; the frame
// keeps the long-image width, so its layout and measured height stay the image's.
const box = ref<HTMLElement | null>(null);
const boxSize = ref({ width: 0, height: 0 });
const resize = new ResizeObserver(([entry]) => {
  boxSize.value = { width: entry.contentRect.width, height: entry.contentRect.height };
});
onMounted(() => resize.observe(box.value!));
onBeforeUnmount(() => {
  controller.abort();
  resize.disconnect();
});
const frameStyle = computed(() => {
  const { width, height } = boxSize.value;
  const scale = Math.min(1, width / IMAGE_WIDTH) || 1;
  return {
    width: `${IMAGE_WIDTH}px`,
    height: `${height / scale}px`,
    left: `${(width - IMAGE_WIDTH * scale) / 2}px`,
    transform: `scale(${scale})`,
  };
});

async function dataUrl(url: string) {
  const response = await fetch(url);
  if (!response.ok) throw Error(`资源加载失败：${response.status} ${url}`);
  const blob = await response.blob();
  return await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

const entries = () =>
  shareEntries(rounds.value, props.session, {
    from: from.value,
    to: to.value,
    alias: alias.value || '用户',
    character: props.name,
  });
const rangeLabel = () => shareRangeLabel(rounds.value, from.value, to.value);

async function build() {
  if (current.value) return current.value;
  controller.abort();
  const signal = (controller = new AbortController()).signal;
  const target = key.value;
  const chosen = entries();
  const style = getComputedStyle(document.documentElement);
  const [portrait, quoteFont] = await Promise.all([
    props.portraitSrc ? dataUrl(props.portraitSrc) : '',
    dataUrl(quoteFontUrl),
  ]);
  let counter = 0;
  const messages = [];
  for (const [index, entry] of chosen.entries()) {
    status.value = `正在整理 ${index + 1}/${chosen.length} 条消息…`;
    const html = await shareMessageHtml(entry, props.session.id, () => ++counter, signal);
    messages.push({ role: entry.role, speaker: entry.speaker, html });
  }
  const html = shareDocument({
    title: props.name,
    portrait,
    range: rangeLabel(),
    css: shareCss({
      palette: Object.fromEntries(PALETTE.map((name) => [name, style.getPropertyValue('--' + name).trim()])),
      reading: {
        fontFamily: String(props.reading['--story-font-family']),
        fontSize: String(props.reading['--story-font-size']),
        lineHeight: String(props.reading['--story-line-height']),
      },
      quoteFont,
    }),
    messages,
  });
  built.value = { key: target, html };
  return html;
}

const fileName = (extension: string) => {
  const range = from.value <= first.value && to.value >= last.value ? '' : `-第${from.value}-${to.value}轮`;
  return `${(props.name || '对话').replace(/[\\/:*?"<>|]/g, '_')}-对话分享${range}${extension}`;
};

async function run(action: () => Promise<string>) {
  if (working.value) return;
  working.value = true;
  error.value = '';
  try {
    status.value = await action();
  } catch (failure) {
    if (controller.signal.aborted && (failure as Error)?.name === 'AbortError') return;
    status.value = '';
    error.value = failure instanceof Error ? failure.message : String(failure);
  } finally {
    working.value = false;
  }
}
const preview = () =>
  run(async () => {
    await build();
    showPreview.value = true;
    return '';
  });
const exportHtml = () =>
  run(async () => {
    const html = await build();
    const saved = await saveDownload(
      new Blob([html], { type: 'text/html;charset=utf-8' }),
      fileName('.html'),
    );
    return saved ? '已导出 HTML 文件。' : '已取消保存。';
  });
const exportText = () =>
  run(async () => {
    const text = shareText({ title: props.name, range: rangeLabel(), entries: entries() });
    const saved = await saveDownload(
      new Blob([text], { type: 'text/plain;charset=utf-8' }),
      fileName('.txt'),
    );
    return saved ? '已导出文本文件。' : '已取消保存。';
  });
const exportImage = () =>
  run(async () => {
    const html = await build();
    status.value = '正在绘制长图…';
    const saved = await saveDownload(await sharePng(html, MAX_IMAGE_HEIGHT), fileName('.png'));
    return saved ? '已导出长图。' : '已取消保存。';
  });
// The preview frame uses the long-image width, so its height is the image's.
function measure(event: Event) {
  const doc = (event.target as HTMLIFrameElement).contentDocument;
  if (doc) previewHeight.value = { key: key.value, height: Math.ceil(doc.documentElement.scrollHeight) };
}
</script>

<template>
  <div class="share-dialog">
    <header class="share-head">
      <h2>分享对话</h2>
      <p>
        生成一份给他人阅读的正文副本，不含设定、记忆与变量。卡片界面保留静态外观，脚本与状态面板不导出；文件不含脚本，打开时不联网。
      </p>
    </header>
    <div class="share-range">
      <template v-for="side in ['from', 'to'] as const" :key="side">
        <Icon v-if="side === 'to'" name="arrow-right" class="share-range-arrow" />
        <label class="share-endpoint">
          <span class="share-endpoint-head"
            ><span class="share-endpoint-label">{{ side === 'from' ? '起始' : '结束' }}</span
            ><span class="share-round"
              >第<input
                v-if="side === 'from'"
                v-model.number="from"
                type="number"
                :min="first"
                :max="last"
                :disabled="working"
              /><input
                v-else
                v-model.number="to"
                type="number"
                :min="first"
                :max="last"
                :disabled="working"
              />轮</span
            ></span
          >
          <span class="share-summary"
            ><strong>{{ endpoint(side === 'from' ? from : to).user }}</strong
            ><span>{{ endpoint(side === 'from' ? from : to).reply }}</span></span
          >
        </label>
      </template>
    </div>
    <label class="share-alias"
      ><span>分享时的称呼</span><input v-model.trim="alias" :disabled="working" placeholder="用户" />
      <small v-if="persona && alias !== persona"
        >正文中出现的“{{ persona }}”也会替换为“{{ alias || '用户' }}”。</small
      >
    </label>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <div ref="box" class="share-preview">
      <iframe
        v-if="showPreview && current"
        title="分享预览"
        sandbox="allow-same-origin"
        :srcdoc="current"
        :style="frameStyle"
        @load="measure"
      />
      <div v-else class="share-preview-empty">
        <Icon name="eye" :size="26" />
        <strong>{{ working ? '正在生成…' : showPreview ? '范围或称呼已改变' : '预览分享效果' }}</strong>
        <span>按长图宽度 {{ IMAGE_WIDTH }} 像素排版；HTML 文件会随浏览器窗口加宽。</span>
        <button :disabled="working || !messageCount" @click="preview">
          {{ showPreview ? '重新生成预览' : '生成预览' }}
        </button>
      </div>
    </div>
    <div class="modal-footer share-footer">
      <p class="share-status" role="status">
        共 {{ messageCount }} 条消息<template v-if="imageHeight">
          · 长图 {{ imageSize }} 像素<span v-if="imageHeight > MAX_IMAGE_HEIGHT" class="share-warning"
            >，超过高度上限 {{ MAX_IMAGE_HEIGHT }}，请缩小范围</span
          ></template
        ><template v-if="status"> · {{ status }}</template>
      </p>
      <button :disabled="working || !messageCount" @click="exportText">导出文本</button
      ><button :disabled="working || !messageCount || imageHeight > MAX_IMAGE_HEIGHT" @click="exportImage">
        导出长图</button
      ><button class="primary" :disabled="working || !messageCount" @click="exportHtml">导出 HTML</button>
    </div>
  </div>
</template>
