import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch, type Ref } from 'vue';
import {
  cardMentionQuery,
  cardReferenceOptions,
  type CardMentionQuery,
  type CardReferenceOption,
  type LibraryReference,
} from '../lib/cardReferences';
import type { EditorCard, EditorPersona } from '../lib/cardEditor';

export function useCardReferencePicker(config: {
  input: Ref<string>;
  composer: Ref<HTMLTextAreaElement | undefined>;
  frame: Ref<HTMLElement | undefined>;
  cards: Ref<EditorCard[]>;
  library: Ref<LibraryReference[]>;
  personas: Ref<EditorPersona[]>;
  attachedPersonas: Ref<Record<string, EditorPersona>>;
  disabled: Ref<boolean>;
  choose: (option: CardReferenceOption) => Promise<void>;
}) {
  const id = useId();
  const query = ref<CardMentionQuery | null>(null);
  const activeIndex = ref(0);
  const maxHeight = ref(300);
  const options = computed(() =>
    cardReferenceOptions(
      config.cards.value,
      config.library.value,
      query.value?.query || '',
      config.personas.value,
      config.attachedPersonas.value,
    ),
  );
  const activeId = computed(() => (options.value.length ? `${id}-${activeIndex.value}` : undefined));
  const open = computed(() => !!query.value);
  let observer: ResizeObserver | undefined;

  function position() {
    const frame = config.frame.value;
    if (frame) maxHeight.value = Math.max(80, Math.min(340, frame.getBoundingClientRect().top - 12));
  }
  function close() {
    query.value = null;
  }
  function update() {
    const textarea = config.composer.value;
    if (!textarea || config.disabled.value) return close();
    const next = cardMentionQuery(config.input.value, textarea.selectionStart, textarea.selectionEnd);
    if (next?.query !== query.value?.query || next?.start !== query.value?.start) activeIndex.value = 0;
    query.value = next;
    position();
  }
  async function show() {
    if (config.disabled.value) return;
    const textarea = config.composer.value;
    if (!textarea) return;
    const caret = textarea.selectionStart;
    if (!cardMentionQuery(config.input.value, caret, textarea.selectionEnd)) {
      const before = config.input.value.slice(0, caret);
      const trigger = `${/\w$/.test(before) ? ' ' : ''}@`;
      config.input.value = before + trigger + config.input.value.slice(caret);
      await nextTick();
      textarea.setSelectionRange(caret + trigger.length, caret + trigger.length);
    }
    textarea.focus({ preventScroll: true });
    update();
  }
  async function complete(original: string, range: CardMentionQuery | null) {
    // Only consume the trigger after the actual attachment succeeded. An error
    // leaves both the query and all unsent prose available for retry.
    if (range && config.input.value === original) {
      config.input.value = original.slice(0, range.start) + original.slice(range.end);
    }
    close();
    await nextTick();
    const textarea = config.composer.value;
    textarea?.focus({ preventScroll: true });
    if (range) textarea?.setSelectionRange(range.start, range.start);
  }
  function keydown(event: KeyboardEvent): boolean {
    if (event.isComposing || event.keyCode === 229) return true;
    if (!open.value) return false;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      close();
      return true;
    }
    if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
      event.preventDefault();
      const count = options.value.length;
      if (count)
        activeIndex.value = (activeIndex.value + (event.key === 'ArrowDown' ? 1 : count - 1)) % count;
      return true;
    }
    if (
      ['Enter', 'Tab'].includes(event.key) &&
      !event.shiftKey &&
      !event.ctrlKey &&
      !event.metaKey &&
      !event.altKey
    ) {
      const option = options.value[activeIndex.value];
      if (event.key === 'Tab' && !option) {
        close();
        return false;
      }
      event.preventDefault();
      if (option && !config.disabled.value) void config.choose(option);
      return true;
    }
    if (event.key === 'Tab') close();
    return false;
  }
  function keyup(event: KeyboardEvent) {
    if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) update();
  }
  function outside(event: PointerEvent) {
    if (!config.frame.value?.contains(event.target as Node)) close();
  }
  watch(options, () => {
    activeIndex.value = Math.min(activeIndex.value, Math.max(0, options.value.length - 1));
  });
  onMounted(() => {
    document.addEventListener('pointerdown', outside);
    window.addEventListener('resize', position);
    observer = new ResizeObserver(position);
    if (config.frame.value) observer.observe(config.frame.value);
  });
  onBeforeUnmount(() => {
    document.removeEventListener('pointerdown', outside);
    window.removeEventListener('resize', position);
    observer?.disconnect();
  });
  return {
    id,
    query,
    open,
    options,
    activeIndex,
    activeId,
    maxHeight,
    show,
    close,
    update,
    complete,
    keydown,
    keyup,
  };
}
