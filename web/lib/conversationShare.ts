import DOMPurify from 'dompurify';
import { streamApi } from './api';
import { historyRounds, type HistoryMessage } from './storyHistory';
import { messageContents, type StoryContent } from './storyPresentation';
import { renderStoryText } from './storyText';

/** A shared copy is for reading only: no script, no network, inline images only. */
const SHARE_CSP =
  "default-src 'none'; img-src data:; media-src data:; font-src data:; style-src 'unsafe-inline'";
/** CSS width of the long-image layout; drawn at 2x for a 1920 px wide image. */
export const IMAGE_WIDTH = 960;
export const IMAGE_SCALE = 2;

export type ShareRound = { number: number; label: string; messages: HistoryMessage[] };
export type ShareEntry = { role: string; speaker: string; message: any; parts: StoryContent[] };
export type ShareTheme = {
  palette: Record<string, string>;
  reading: { fontFamily: string; fontSize: string; lineHeight: string };
  quoteFont: string;
};

/** Share-facing rounds start at 1, including the opening. */
export function shareRounds(messages: HistoryMessage[]): ShareRound[] {
  return historyRounds(messages).map((round, index) => {
    const number = index + 1;
    return {
      number,
      label: `第 ${number} 轮`,
      messages: [round.user, round.reply].filter((message): message is HistoryMessage => !!message),
    };
  });
}

export function shareRangeLabel(rounds: ShareRound[], from: number, to: number) {
  const last = rounds.at(-1)?.number ?? 0;
  if (from <= (rounds[0]?.number ?? 1) && to >= last) return `共 ${rounds.length} 轮`;
  if (from === to) return `第 ${from} 轮`;
  return `第 ${from}–${to} 轮`;
}

/** Only the reading text of the chosen rounds: display parts with macros expanded,
 * the persona renamed to `alias`, and the status panel left out. */
export function shareEntries(
  rounds: ShareRound[],
  session: any,
  { from, to, alias, character }: { from: number; to: number; alias: string; character: string },
): ShareEntry[] {
  const persona = String(session.persona || '').trim();
  const rename = (text: string) => (persona && alias !== persona ? text.split(persona).join(alias) : text);
  return rounds
    .filter((round) => round.number >= from && round.number <= to)
    .flatMap((round) => round.messages)
    .map((message) => ({
      role: message.role,
      speaker: message.role === 'user' ? alias : character,
      message,
      parts: messageContents(message, session, { persona: alias })
        .map((part) => ({ ...part, text: rename(part.text) }))
        .filter((part) => part.format === 'html' || part.text.trim()),
    }))
    .filter((entry) => entry.parts.length);
}

/** Rendered prose flattened to paragraphs separated by blank lines. */
function plainText(html: string) {
  const root = document.createElement('template');
  root.innerHTML = html;
  const content = root.content;
  // Newlines between block tags are markup layout, not text (except inside <pre>).
  const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
  const gaps: Text[] = [];
  while (walker.nextNode()) {
    const node = walker.currentNode as Text;
    if (!node.data.trim() && node.data.includes('\n') && !node.parentElement?.closest('pre')) gaps.push(node);
  }
  gaps.forEach((node) => node.remove());
  content.querySelectorAll('br').forEach((node) => node.replaceWith('\n'));
  content.querySelectorAll('img').forEach((node) => node.replaceWith('[图片]'));
  content.querySelectorAll('ul > li').forEach((node) => node.prepend('· '));
  content.querySelectorAll('ol').forEach((list) => {
    const start = Number(list.getAttribute('start') || 1);
    list.querySelectorAll(':scope > li').forEach((node, index) => node.prepend(`${start + index}. `));
  });
  content.querySelectorAll('td, th').forEach((node) => node.append('\t'));
  content.querySelectorAll('li, tr').forEach((node) => node.append('\n'));
  content
    .querySelectorAll('p, h1, h2, h3, h4, h5, h6, blockquote, pre, ul, ol, table, hr, div')
    .forEach((node) => node.append('\n\n'));
  return (content.textContent || '')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

/** Plain-text copy of the chosen rounds. Like the copy button, card interfaces
 * stay behind; a message made only of them is left out. */
export function shareText({
  title,
  range,
  entries,
}: {
  title: string;
  range: string;
  entries: ShareEntry[];
}) {
  const messages = entries.flatMap((entry) => {
    const text = entry.parts
      .filter((part) => part.format === 'markdown')
      .map((part) => plainText(renderStoryText(part.text, entry.role === 'user')))
      .filter(Boolean)
      .join('\n\n');
    return text ? [`【${entry.speaker}】\n${text}`] : [];
  });
  return `${title}\n${range}\n\n${messages.join('\n\n')}\n`;
}

const escapeHtml = (text: string) =>
  text.replace(/[&<>"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[char]!);

/** The complete self-contained file. Message bodies arrive already sanitized. */
export function shareDocument({
  title,
  portrait,
  range,
  css,
  messages,
}: {
  title: string;
  portrait: string;
  range: string;
  css: string;
  messages: { role: string; speaker: string; html: string }[];
}) {
  const avatar = portrait ? `<img src="${escapeHtml(portrait)}" alt="">` : '';
  return `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${SHARE_CSP}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title><style>${css}</style></head>
<body><main class="share"><header class="share-head">${avatar}<div><h1>${escapeHtml(title)}</h1><p>${escapeHtml(range)}</p></div></header>
${messages
  .map(
    (message) =>
      `<article class="share-message ${message.role === 'user' ? 'user' : 'assistant'}"><div class="share-speaker">${escapeHtml(message.speaker)}</div><div class="prose">${message.html}</div></article>`,
  )
  .join('\n')}
</main></body></html>`;
}

export function shareCss({ palette: p, reading, quoteFont }: ShareTheme) {
  return `${quoteFont ? `@font-face{font-family:'Tavern Quote Marks';src:url('${quoteFont}') format('truetype');unicode-range:U+2018-2019,U+201C-201D}` : ''}
*{box-sizing:border-box}
html{color-scheme:dark}
body{margin:0;background:${p['surface-base']};color:${p['text-story']}}
.share{max-width:1160px;margin:0 auto;padding:44px 48px 56px;font-family:'Tavern Quote Marks',${reading.fontFamily};font-size:${reading.fontSize};line-height:${reading.lineHeight};overflow-wrap:anywhere}
.share-head{display:flex;align-items:center;gap:16px;padding-bottom:22px;margin-bottom:30px;border-bottom:1px solid ${p['border-strong']}}
.share-head img{width:64px;height:64px;border-radius:50%;object-fit:cover}
.share-head h1{margin:0;font-size:1.25em;line-height:1.4;color:${p['text-primary']}}
.share-head p{margin:2px 0 0;font-size:.8em;color:${p['text-muted']}}
.share-message{margin:0 0 1.6em}
.share-speaker{margin-bottom:.3em;font-size:.78em;font-weight:600;color:${p['text-muted']}}
.share-message.user .prose{padding-left:.9em;border-left:2px solid ${p['border-strong']};color:${p['text-secondary']}}
.prose p{margin:0 0 .8em}
.prose em{color:${p['text-secondary']}}
.prose strong,.prose h1,.prose h2,.prose h3,.prose h4,.prose h5,.prose h6{color:${p['text-primary']};font-weight:600}
.prose h1,.prose h2,.prose h3,.prose h4,.prose h5,.prose h6{line-height:1.45;margin:1.4em 0 .65em;font-size:1.1em}
.prose blockquote{margin-left:0;padding-left:15px;border-left:2px solid ${p['border-strong']};color:${p['text-secondary']}}
.prose img{max-width:100%;border-radius:6px}
.prose .story-dialogue{color:${p['text-dialogue']}}
.prose a{color:${p['accent-hover']}}
.prose ul,.prose ol{padding-left:1.6em;margin:.7em 0 1em}
.prose pre{white-space:pre-wrap;padding:.6em .8em;background:${p['surface-hover']};color:${p['text-secondary']}}
.prose code{font-size:.85em}
.prose table{border-collapse:collapse;font-size:.9em;margin:1em 0}
.prose th,.prose td{border:1px solid ${p['border-strong']};padding:.3em .6em}
.share-missing{color:${p['text-muted']}}
.share-card{margin:.6em 0;overflow:hidden}
@media (max-width:620px){.share{padding:28px 18px 40px}}`;
}

/** Card CSS stays inside its own block: rules are wrapped in @scope, page-level
 * selectors map to the block, and fonts or imports (never loadable here) are dropped. */
function scopeStyle(css: string, scope: string) {
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(css); // Ignores @import by specification.
  const scoped: string[] = [],
    global: string[] = [];
  for (const rule of Array.from(sheet.cssRules)) {
    if (rule instanceof CSSFontFaceRule) continue;
    if (rule instanceof CSSKeyframesRule) global.push(rule.cssText);
    else if (rule instanceof CSSStyleRule)
      scoped.push(
        rule.cssText.replace(
          rule.selectorText,
          rule.selectorText.replace(/(^|[\s,>+~(])(?:html|body|:root)(?=$|[\s,.:#[>+~)])/g, '$1:scope'),
        ),
      );
    else scoped.push(rule.cssText);
  }
  return `${global.join('\n')}\n@scope (.${scope}) {\n${scoped.join('\n')}\n}`;
}

/** Remote images come back through the host, like the reader's own prose. */
async function inlineImages(html: string, sessionId: string, messageId: string, signal: AbortSignal) {
  if (!/https?:\/\//i.test(html)) return html;
  const loaded = new Map<string, string>();
  let prepared = html;
  try {
    await streamApi(
      `/sessions/${sessionId}/images/stream`,
      { html, message_id: messageId },
      signal,
      (line) => {
        if (line.html !== undefined) prepared = line.html;
        else if (line.data !== undefined) loaded.set(line.url, line.data);
      },
    );
  } catch (error) {
    if (signal.aborted) throw error;
    // The host rejects the whole block when a card references a resource it never
    // declared; the reader shows that as an image failure, so the copy marks them too.
    return html;
  }
  for (const [url, data] of loaded) prepared = prepared.split(url).join(data);
  return prepared;
}

/** One message's static HTML: Markdown rendered like the reader, card interfaces
 * kept without scripts, styles scoped, and unloadable images marked in place. */
export async function shareMessageHtml(
  entry: ShareEntry,
  sessionId: string,
  next: () => number,
  signal: AbortSignal,
) {
  const source = entry.parts
    .map((part) =>
      part.format === 'markdown'
        ? renderStoryText(part.text, entry.role === 'user')
        : `<div class="share-card">${part.text}</div>`,
    )
    .join('');
  const root = document.createElement('template');
  root.innerHTML = DOMPurify.sanitize(source, { FORCE_BODY: true });
  root.content.querySelectorAll('.share-card').forEach((card) => {
    const scope = `share-card-${next()}`;
    card.classList.add(scope);
    card.querySelectorAll('style').forEach((style) => {
      style.textContent = scopeStyle(style.textContent || '', scope);
    });
  });
  // Scripts, stylesheet links, @import and fonts are gone before the host sees the
  // markup: it rejects external resources a card never declared.
  root.innerHTML = await inlineImages(root.innerHTML, sessionId, entry.message.id, signal);
  root.content.querySelectorAll('img').forEach((image) => {
    if (image.getAttribute('src')?.startsWith('data:')) return;
    const missing = document.createElement('span');
    missing.className = 'share-missing';
    missing.textContent = '[图片]';
    image.replaceWith(missing);
  });
  return root.innerHTML;
}

/** Draws the share document as a PNG via SVG foreignObject; no library needed
 * because the document is already self-contained. */
export async function sharePng(documentHtml: string, maxHeight: number) {
  const frame = document.createElement('iframe');
  frame.setAttribute('sandbox', 'allow-same-origin');
  frame.style.cssText = `position:fixed;left:-20000px;top:0;width:${IMAGE_WIDTH}px;height:100px;border:0;visibility:hidden`;
  const loaded = new Promise((resolve) => frame.addEventListener('load', resolve, { once: true }));
  frame.srcdoc = documentHtml;
  document.body.append(frame);
  try {
    await loaded;
    const doc = frame.contentDocument!;
    await doc.fonts.ready;
    const height = Math.ceil(doc.documentElement.scrollHeight);
    if (height * IMAGE_SCALE > maxHeight) {
      throw Error(`长图高度约 ${height * IMAGE_SCALE} 像素，超过上限 ${maxHeight}，请缩小轮次范围`);
    }
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${IMAGE_WIDTH * IMAGE_SCALE}" height="${height * IMAGE_SCALE}" viewBox="0 0 ${IMAGE_WIDTH} ${height}"><foreignObject width="${IMAGE_WIDTH}" height="${height}">${new XMLSerializer().serializeToString(doc.documentElement)}</foreignObject></svg>`;
    const image = new Image();
    image.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
    await image.decode();
    const canvas = document.createElement('canvas');
    canvas.width = IMAGE_WIDTH * IMAGE_SCALE;
    canvas.height = height * IMAGE_SCALE;
    canvas.getContext('2d')!.drawImage(image, 0, 0);
    return await new Promise<Blob>((resolve, reject) =>
      canvas.toBlob((blob) => (blob ? resolve(blob) : reject(Error('长图生成失败'))), 'image/png'),
    );
  } finally {
    frame.remove();
  }
}
