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
