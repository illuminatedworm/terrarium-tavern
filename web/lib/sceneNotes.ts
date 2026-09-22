export function archiveSourceText(message: any, persona = '', character = ''): string {
  const document = message.snapshot?.content?.document;
  const card = document?.data || document;
  const name = card?.nickname || card?.name || character;
  return String(message.model ?? message.raw ?? '').replace(/\{\{([^{}]+)}}/g, (match, token: string) => {
    const key = token.trim();
    if (key === 'user') return persona;
    if (key === 'char') return name;
    if (key.startsWith('getvar::') || key.startsWith('getglobalvar::')) {
      let value = message.snapshot?.state;
      for (const part of key.split('::').slice(1).join('::').split('.')) {
        value = value != null && Object.hasOwn(value, part) ? value[part] : undefined;
      }
      return value == null ? match : String(value);
    }
    return match;
  });
}

export function highlightSource(root: HTMLElement, excerpt: string): boolean {
  const marks = root.querySelectorAll('mark.source-sentence-highlight');
  marks.forEach((mark) => mark.replaceWith(...Array.from(mark.childNodes)));
  root.normalize();
  if (!excerpt.trim()) return false;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes: Text[] = [];
  let next: Node | null;
  while ((next = walker.nextNode())) nodes.push(next as Text);
  const text = nodes.map((node) => node.data).join('');
  const start = text.indexOf(excerpt);
  if (start < 0) return false;
  let offset = 0;
  for (const node of nodes) {
    const from = Math.max(0, start - offset),
      to = Math.min(node.length, start + excerpt.length - offset);
    offset += node.length;
    if (to <= from) continue;
    const range = document.createRange();
    range.setStart(node, from);
    range.setEnd(node, to);
    const mark = document.createElement('mark');
    mark.className = 'source-sentence-highlight';
    range.surroundContents(mark);
  }
  root.querySelector('.source-sentence-highlight')?.scrollIntoView({ block: 'center' });
  return true;
}
