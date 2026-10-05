export function positionPopover(popup: HTMLElement, anchor: HTMLElement) {
  const target = anchor.getBoundingClientRect();
  const box = popup.getBoundingClientRect();
  const gutter = 8;
  popup.style.left = `${Math.max(gutter, Math.min(target.right - box.width, innerWidth - box.width - gutter))}px`;
  const below = target.bottom + box.height + gutter <= innerHeight;
  popup.style.top = `${Math.max(gutter, Math.min(below ? target.bottom + 6 : target.top - box.height - 6, innerHeight - box.height - gutter))}px`;
}
