import { marked } from 'marked';

function hasMvuScript(text: string): boolean {
  return [...text.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script\s*>/gi)].some((match) =>
    /stat_data|getAllVariables|getMvuData|StatusPlaceHolder/i.test(match[1]),
  );
}
const voidTags = new Set('area base br col embed hr img input link meta param source track wbr'.split(' '));
type Fragment = { start: number; end: number; html: string; fenced?: boolean };
export type StoryContent = { format: 'markdown' | 'html'; text: string };
export type StoryPresentation = { body: StoryContent[]; panel: StoryContent[] };
const isInterface = (html: string) =>
  /<(?:html|!doctype|head|body|script|style|div|section|details|main|article|table|form)\b/i.test(html);

/** Keep source offsets so moving a panel never truncates or rewrites surrounding prose. */
function htmlFragments(text: string, offset: number): Fragment[] {
  const tags = /<!--[\s\S]*?-->|<!doctype\b[^>]*>|<\/?([a-z][\w:-]*)\b(?:[^>"']|"[^"]*"|'[^']*')*>/gi;
  const fragments: Fragment[] = [];
  const stack: string[] = [];
  let start = -1;
  for (let tag = tags.exec(text); tag; tag = tags.exec(text)) {
    const name = tag[1]?.toLowerCase();
    const closing = tag[0].startsWith('</');
    if (!stack.length) {
      if (closing) continue;
      start = tag.index;
    }
    if (name && !closing && !voidTags.has(name) && !tag[0].endsWith('/>')) {
      if (['script', 'style', 'textarea', 'title'].includes(name)) {
        // JavaScript/CSS strings can contain markup; only their closing tag ends the element.
        const close = new RegExp(`</${name}\\s*>`, 'gi');
        close.lastIndex = tags.lastIndex;
        if (!close.exec(text)) break;
        tags.lastIndex = close.lastIndex;
      } else stack.push(name);
    } else if (name && closing) {
      const ancestor = stack.lastIndexOf(name);
      if (ancestor < 0) continue;
      // HTML permits omitted descendant end tags, e.g. <table><tr><td>…</table>.
      // A real enclosing end tag still gives us a lossless source boundary.
      stack.splice(ancestor);
    }
    if (!stack.length) {
      const end = tags.lastIndex;
      fragments.push({ start: offset + start, end: offset + end, html: text.slice(start, end) });
    }
  }
  return fragments;
}

function displayFragments(text: string): Fragment[] {
  const fragments: Fragment[] = [];
  let offset = 0;
  // Match marked's newline normalization while retaining offsets into the original message.
  const normalized = text.replace(/\r\n|\r/g, '\n');
  const removed: number[] = [];
  for (let i = 0; i < text.length; i++) {
    if (text[i] === '\r' && text[i + 1] === '\n') {
      removed.push(i - removed.length);
      i++;
    }
  }
  // Store only collapsed CRLF positions, not one boxed number per source character.
  const originalOffset = (position: number) => {
    let low = 0,
      high = removed.length;
    while (low < high) {
      const mid = (low + high) >>> 1;
      if (removed[mid] < position) low = mid + 1;
      else high = mid;
    }
    return position + low;
  };
  let rawStart = 0;
  for (const token of marked.lexer(normalized)) {
    // Only fences delimit Markdown from raw HTML. Marked splits a raw <head> or
    // <body> at blank lines and labels indented CSS/JS as code; those token
    // boundaries are not HTML boundaries and must not cut the document apart.
    if (token.type === 'code' && /^ {0,3}(?:`{3,}|~{3,})/.test(token.raw)) {
      fragments.push(...htmlFragments(normalized.slice(rawStart, offset), rawStart));
      if ((!token.lang || token.lang.toLowerCase() === 'html') && isInterface(token.text)) {
        fragments.push({ start: offset, end: offset + token.raw.length, html: token.text, fenced: true });
      }
      rawStart = offset + token.raw.length;
    }
    offset += token.raw.length;
  }
  fragments.push(...htmlFragments(normalized.slice(rawStart), rawStart));
  // Adjacent roots form one interface (e.g. style + details + script). Meaningful text
  // between roots ends that interface, and must stay in the conversation.
  const groups: Fragment[] = [];
  for (const fragment of fragments) {
    const previous = groups.at(-1);
    const gap = previous ? normalized.slice(previous.end, fragment.start) : '';
    if (previous && !previous.fenced && !fragment.fenced && !gap.trim()) {
      previous.html += gap + fragment.html;
      previous.end = fragment.end;
    } else groups.push({ ...fragment });
  }
  return groups
    .filter((group) => isInterface(group.html))
    .map((group) => ({ ...group, start: originalOffset(group.start), end: originalOffset(group.end) }));
}

export function htmlDocuments(text: string): string[] {
  return displayFragments(text).map((fragment) => fragment.html);
}

export function splitMvuPanel(text: string): { panel: string; narrative: string } {
  if (!hasMvuScript(text)) return { panel: '', narrative: text };
  const panels = displayFragments(text).filter((group) => hasMvuScript(group.html));
  let narrative = '';
  let cursor = 0;
  for (const panel of panels) {
    narrative += text.slice(cursor, panel.start);
    cursor = panel.end;
  }
  narrative += text.slice(cursor);
  return { panel: panels.map((part) => part.html).join('\n'), narrative };
}

/** One structural parser for the UI, message callbacks and Card Editor probes.
 * CSS-only and ordinary HTML stay inline; recognized MVU scripts move to the
 * sidebar. Both paths preserve all surrounding prose and every HTML block.
 */
export function parseStoryDisplay(text: string, role = 'assistant'): StoryPresentation {
  if (!/<[a-z!]/i.test(text)) return { body: [{ format: 'markdown', text }], panel: [] };
  const body: StoryContent[] = [],
    panel: StoryContent[] = [];
  let cursor = 0;
  for (const fragment of displayFragments(text)) {
    if (fragment.start > cursor) body.push({ format: 'markdown', text: text.slice(cursor, fragment.start) });
    const target = role === 'assistant' && hasMvuScript(fragment.html) ? panel : body;
    target.push({ format: 'html', text: fragment.html });
    cursor = fragment.end;
  }
  if (cursor < text.length || !body.length) body.push({ format: 'markdown', text: text.slice(cursor) });
  return { body, panel };
}
