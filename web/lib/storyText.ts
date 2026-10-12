import DOMPurify from 'dompurify';
import { parseStoryMarkdown } from './storyMarkdown';

export function openingProse(source: string): string {
  return source
    .replace(/<UpdateVariable\b[^>]*>[\s\S]*?<\/UpdateVariable\s*>/gi, '')
    .replace(/<initvar\b[^>]*>[\s\S]*?<\/initvar\s*>/gi, '')
    .replace(/<StatusPlaceHolderImpl\s*\/>/gi, '');
}

export function dialogueRanges(text: string): Array<[number, number]> {
  // Word boundaries keep English contractions and possessives out of dialogue.
  const quotes =
    /“[^“”\n]+”|「[^「」\n]+」|『[^『』\n]+』|"[^"\n]+"|‘[^‘’\n]+’|(?<![\p{L}\p{N}])'(?:[^'\n]|(?<=[\p{L}\p{N}])'(?=[\p{L}\p{N}]))+'(?![\p{L}\p{N}])/gu;
  return Array.from(text.matchAll(quotes), (match) => [match.index!, match.index! + match[0].length]);
}

export function renderStoryText(source: string, breaks = false): string {
  const html = DOMPurify.sanitize(parseStoryMarkdown(source, breaks));
  const root = document.createElement('div');
  root.innerHTML = html;
  const groups = new Map<Element, Text[]>();
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node: Node | null;
  while ((node = walker.nextNode())) {
    const text = node as Text;
    const block =
      text.parentElement?.closest('p, li, h1, h2, h3, h4, h5, h6, td, th, blockquote, div') || root;
    const group = groups.get(block) || [];
    group.push(text);
    groups.set(block, group);
  }
  for (const nodes of groups.values()) {
    const excluded = (text: Text) =>
      !!text.parentElement?.closest('pre, code, kbd, samp, script, style, textarea');
    const fullText = nodes.map((text) => (excluded(text) ? '\n' : text.data)).join('');
    const ranges = dialogueRanges(fullText);
    let offset = 0;
    for (const text of nodes) {
      if (excluded(text)) {
        offset += 1;
        continue;
      }
      const start = offset;
      offset += text.length;
      const hits = ranges.filter(([a, b]) => a < offset && b > start);
      if (!hits.length) continue;
      const fragment = document.createDocumentFragment();
      let position = 0;
      for (const [a, b] of hits) {
        const from = Math.max(0, a - start),
          to = Math.min(text.length, b - start);
        fragment.append(text.data.slice(position, from));
        const span = document.createElement('span');
        span.className = 'story-dialogue';
        span.textContent = text.data.slice(from, to);
        fragment.append(span);
        position = to;
      }
      fragment.append(text.data.slice(position));
      text.replaceWith(fragment);
    }
  }
  return root.innerHTML;
}
