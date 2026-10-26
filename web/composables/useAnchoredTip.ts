import { nextTick, onBeforeUnmount, ref, type CSSProperties } from 'vue';

/** Hover/focus tooltip teleported to body; prefers above the anchor and stays inside the viewport. */
export function useAnchoredTip() {
  const anchor = ref<HTMLElement>();
  const bubble = ref<HTMLElement>();
  const open = ref(false);
  const position = ref<CSSProperties>({ left: '0px', top: '0px', visibility: 'hidden' });
  let hovered = false;
  let focused = false;

  function place() {
    if (!anchor.value || !bubble.value) return;
    const rect = anchor.value.getBoundingClientRect();
    const { width, height } = bubble.value.getBoundingClientRect();
    const left = Math.max(8, Math.min(rect.left + rect.width / 2 - width / 2, innerWidth - width - 8));
    const above = rect.top - height - 8;
    const top = Math.max(8, Math.min(above >= 8 ? above : rect.bottom + 8, innerHeight - height - 8));
    position.value = { left: `${left}px`, top: `${top}px`, visibility: 'visible' };
  }
  function unlisten() {
    window.removeEventListener('resize', place);
    window.removeEventListener('scroll', place, true);
  }
  async function update() {
    open.value = hovered || focused;
    if (!open.value) return unlisten();
    position.value.visibility = 'hidden';
    await nextTick();
    place();
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
  }
  const listeners = {
    mouseenter: () => ((hovered = true), update()),
    mouseleave: () => ((hovered = false), update()),
    focus: () => ((focused = true), update()),
    blur: () => ((focused = false), update()),
    keydown: (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      hovered = focused = false;
      update();
    },
  };
  onBeforeUnmount(unlisten);
  return { anchor, bubble, open, position, listeners };
}
