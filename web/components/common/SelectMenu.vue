<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, useId, watch } from 'vue';
import { positionPopover } from '../../lib/anchoredPopover';

export interface SelectMenuOption {
  value: string;
  label: string;
  description?: string;
  disabled?: boolean;
}
const props = withDefaults(
  defineProps<{
    modelValue: string;
    options: SelectMenuOption[];
    label: string;
    caption?: string;
    placeholder?: string;
    disabled?: boolean;
    invalid?: boolean;
    compact?: boolean;
    highlighted?: boolean;
    searchable?: boolean;
    allowCustom?: boolean;
    searchPlaceholder?: string;
    emptyText?: string;
    error?: string;
  }>(),
  {
    placeholder: '请选择',
    searchPlaceholder: '搜索…',
    emptyText: '暂无可选项',
  },
);
const emit = defineEmits<{ 'update:modelValue': [value: string] }>();
const id = useId();
const trigger = ref<HTMLButtonElement>();
const popup = ref<HTMLElement>();
const search = ref<HTMLInputElement>();
const list = ref<HTMLElement>();
const open = ref(false);
const query = ref('');
const active = ref(-1);
const selected = computed(() => props.options.find((option) => option.value === props.modelValue));
const selectedLabel = computed(() => selected.value?.label || props.modelValue || props.placeholder);
const filtered = computed(() => {
  const needle = query.value.trim().toLocaleLowerCase();
  return props.options.filter((option) =>
    `${option.label} ${option.value} ${option.description || ''}`.toLocaleLowerCase().includes(needle),
  );
});
const custom = computed(
  () =>
    props.allowCustom &&
    !!query.value.trim() &&
    !props.options.some((option) => option.value === query.value.trim()),
);
const choices = computed<SelectMenuOption[]>(() =>
  custom.value
    ? [
        ...filtered.value,
        { value: query.value.trim(), label: query.value.trim(), description: '使用此模型 ID' },
      ]
    : filtered.value,
);
const activeId = computed(() => (active.value >= 0 ? `${id}-option-${active.value}` : undefined));
const enabledIndexes = computed(() =>
  choices.value.flatMap((option, index) => (option.disabled ? [] : [index])),
);

function resetActive() {
  const current = choices.value.findIndex((option) => option.value === props.modelValue && !option.disabled);
  active.value = current >= 0 ? current : choices.value.findIndex((option) => !option.disabled);
}
function position() {
  if (!popup.value?.matches(':popover-open') || !trigger.value) return;
  const box = trigger.value.getBoundingClientRect();
  const space = Math.max(box.top, innerHeight - box.bottom) - 16;
  popup.value.style.maxHeight = `${Math.min(420, Math.max(100, space))}px`;
  positionPopover(popup.value, trigger.value);
}
// Scroll only the option list. scrollIntoView would also scroll the page section
// that contains the popover in the DOM, and an outside scroll closes the menu.
function scrollActive() {
  const box = list.value;
  const option = box?.querySelector<HTMLElement>(`[id="${activeId.value}"]`);
  if (!box || !option) return;
  const view = box.getBoundingClientRect();
  const item = option.getBoundingClientRect();
  if (item.top < view.top) box.scrollTop -= view.top - item.top;
  else if (item.bottom > view.bottom) box.scrollTop += item.bottom - view.bottom;
}
function close(restoreFocus = false) {
  popup.value?.hidePopover();
  open.value = false;
  if (restoreFocus) trigger.value?.focus({ preventScroll: true });
}
async function show(last = false) {
  if (props.disabled || !popup.value) return;
  // Search is a fresh filter on every opening, never a draft of the selected ID.
  query.value = '';
  resetActive();
  if (last && !selected.value) active.value = enabledIndexes.value.at(-1) ?? -1;
  popup.value.showPopover();
  open.value = true;
  await nextTick();
  if (!open.value) return;
  position();
  (props.searchable ? search.value : list.value)?.focus({ preventScroll: true });
  scrollActive();
}
function select(index: number) {
  const option = choices.value[index];
  if (props.disabled || !option || option.disabled) return;
  close(true);
  emit('update:modelValue', option.value);
}
function move(step: number) {
  const enabled = enabledIndexes.value;
  if (!enabled.length) return;
  const current = enabled.indexOf(active.value);
  active.value = enabled[(current + step + enabled.length) % enabled.length];
  scrollActive();
}
function keydown(event: KeyboardEvent) {
  if (event.isComposing) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    event.stopPropagation();
    close(true);
  } else if (event.key === 'Tab') {
    // Tab moves natively between heading actions and the search field; leaving
    // the popup restores the trigger before the native Tab action so the
    // surrounding form (including its modal focus boundary) keeps its order.
    const stops = Array.from(
      popup.value?.querySelectorAll<HTMLElement>('button, input, [tabindex="0"]') ?? [],
    );
    const next = stops.indexOf(event.target as HTMLElement) + (event.shiftKey ? -1 : 1);
    if (next < 0 || next >= stops.length) close(true);
  } else if (event.target instanceof HTMLButtonElement) {
    // Heading actions keep their native Enter and Space activation.
    return;
  } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    move(event.key === 'ArrowDown' ? 1 : -1);
  } else if (event.key === 'Enter' || (event.key === ' ' && !props.searchable)) {
    event.preventDefault();
    select(active.value);
  } else if (!props.searchable && (event.key === 'Home' || event.key === 'End')) {
    event.preventDefault();
    active.value =
      event.key === 'Home'
        ? choices.value.findIndex((option) => !option.disabled)
        : (enabledIndexes.value.at(-1) ?? -1);
    scrollActive();
  }
}
// The menu follows its trigger while the surrounding page scrolls and closes only
// once the trigger leaves the scrolled area. Scroll events arrive a frame late, so
// closing on any scroll would also dismiss a menu opened right after a scroll.
function onScroll(event: Event) {
  if (event.target instanceof Node && popup.value?.contains(event.target)) return;
  const box = trigger.value?.getBoundingClientRect();
  const area =
    event.target instanceof Element
      ? event.target.getBoundingClientRect()
      : { top: 0, left: 0, bottom: window.innerHeight, right: window.innerWidth };
  if (
    !box ||
    box.bottom < area.top ||
    box.top > area.bottom ||
    box.right < area.left ||
    box.left > area.right
  )
    close();
  else position();
}
function toggled(event: Event) {
  open.value = (event as ToggleEvent).newState === 'open';
}
watch(query, async () => {
  active.value = choices.value.findIndex((option) => !option.disabled);
  await nextTick();
  position();
  scrollActive();
});
watch(
  () => props.options,
  async () => {
    if (!open.value) return;
    resetActive();
    await nextTick();
    position();
    scrollActive();
  },
);
watch(
  () => props.disabled,
  (disabled) => {
    if (disabled) close();
  },
);
watch(open, (visible) => {
  if (visible) {
    window.addEventListener('resize', position);
    document.addEventListener('scroll', onScroll, true);
  } else {
    window.removeEventListener('resize', position);
    document.removeEventListener('scroll', onScroll, true);
  }
});
onBeforeUnmount(() => {
  window.removeEventListener('resize', position);
  document.removeEventListener('scroll', onScroll, true);
});
</script>

<template>
  <div class="select-menu" :class="{ compact, highlighted }">
    <button
      ref="trigger"
      type="button"
      class="select-menu-trigger"
      :class="{ open }"
      :disabled="disabled"
      :data-value="modelValue"
      :aria-invalid="invalid || undefined"
      :aria-label="label"
      :aria-haspopup="searchable ? 'dialog' : 'listbox'"
      :aria-expanded="open"
      :aria-controls="`${id}-popup`"
      :title="[selectedLabel, selected?.description].filter(Boolean).join(' · ')"
      @click="open ? close(true) : show()"
      @keydown.down.prevent="show()"
      @keydown.up.prevent="show(true)"
    >
      <span v-if="$slots.icon" class="select-menu-icon"><slot name="icon" /></span>
      <span v-if="caption" class="select-menu-caption">{{ caption }}</span>
      <span class="select-menu-value">{{ selectedLabel }}</span>
      <svg
        class="select-menu-chevron"
        width="12"
        height="12"
        viewBox="0 0 12 12"
        fill="none"
        aria-hidden="true"
      >
        <path
          d="m3 4.5 3 3 3-3"
          stroke="currentColor"
          stroke-width="1.4"
          stroke-linecap="round"
          stroke-linejoin="round"
        />
      </svg>
    </button>
    <div
      :id="`${id}-popup`"
      ref="popup"
      popover="auto"
      class="select-menu-popup"
      :class="{ searchable }"
      :role="searchable ? 'dialog' : undefined"
      :aria-label="label"
      @toggle="toggled"
      @keydown="keydown"
    >
      <div class="select-menu-heading" :class="{ 'has-action': $slots['heading-action'] }">
        <span>{{ caption || label }}</span
        ><slot name="heading-action" />
      </div>
      <slot name="context" />
      <div v-if="searchable" class="select-menu-search">
        <svg width="16" height="16" viewBox="0 0 20 20" fill="none" aria-hidden="true">
          <circle cx="8.5" cy="8.5" r="5.5" stroke="currentColor" stroke-width="1.4" />
          <path d="m13 13 4 4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
        </svg>
        <input
          ref="search"
          v-model="query"
          role="combobox"
          :aria-label="searchPlaceholder"
          aria-autocomplete="list"
          :aria-expanded="open"
          :aria-controls="`${id}-list`"
          :aria-activedescendant="activeId"
          :placeholder="searchPlaceholder"
          autocomplete="off"
          spellcheck="false"
        />
      </div>
      <div
        :id="`${id}-list`"
        ref="list"
        class="select-menu-list"
        role="listbox"
        :aria-label="label"
        :tabindex="searchable ? -1 : 0"
        :aria-activedescendant="searchable ? undefined : activeId"
      >
        <div v-if="error" class="select-menu-error" role="status">{{ error }}</div>
        <div
          v-for="(option, index) in choices"
          :id="`${id}-option-${index}`"
          :key="option.value"
          :data-value="option.value"
          role="option"
          class="select-menu-option"
          :class="{ active: index === active, custom: custom && index === choices.length - 1 }"
          :aria-selected="option.value === modelValue"
          :aria-disabled="option.disabled || undefined"
          @pointermove="!option.disabled && (active = index)"
          @mousedown.prevent
          @click.stop="select(index)"
        >
          <span class="select-menu-option-copy">
            <span>{{ option.label }}</span>
            <small v-if="option.description">{{ option.description }}</small>
          </span>
          <svg
            v-if="option.value === modelValue"
            class="select-menu-check"
            width="16"
            height="16"
            viewBox="0 0 20 20"
            fill="none"
            aria-hidden="true"
          >
            <path
              d="m4 10 4 4 8-8"
              stroke="currentColor"
              stroke-width="1.6"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
        </div>
        <div v-if="!choices.length" class="select-menu-empty" role="status">
          {{ query.trim() ? '没有匹配项' : emptyText }}
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.select-menu {
  min-width: 0;
}
.select-menu .select-menu-trigger {
  display: flex;
  align-items: center;
  gap: 7px;
  width: 100%;
  min-width: 0;
  min-height: 36px;
  margin: 0;
  padding: 7px 10px;
  border: 1px solid var(--border-control);
  border-radius: var(--radius-sm);
  background: rgb(var(--well-rgb) / 0.42);
  color: var(--text-secondary);
  font: var(--fs-md) / 1.5 var(--font-ui);
  text-align: left;
  cursor: pointer;
}
.select-menu.compact .select-menu-trigger {
  min-height: 30px;
  padding: 5px 9px;
  border-color: transparent;
  border-radius: var(--radius-sm);
  background: transparent;
  font: var(--fs-sm) / 1.5 var(--font-ui);
}
.select-menu .select-menu-trigger:hover:not(:disabled),
.select-menu .select-menu-trigger.open {
  background: rgb(255 255 255 / 0.07);
  border-color: var(--glass-edge-strong);
}
.select-menu.compact .select-menu-trigger:hover:not(:disabled),
.select-menu.compact .select-menu-trigger.open {
  border-color: transparent;
}
.select-menu-trigger:disabled {
  cursor: not-allowed;
}
.select-menu-value {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.select-menu-icon {
  display: flex;
  flex: none;
  color: var(--text-muted);
}
.select-menu-caption {
  flex: none;
  padding-right: 7px;
  border-right: 1px solid var(--border-control);
  color: var(--text-muted);
}
.select-menu-chevron {
  flex: none;
  margin-left: auto;
  color: var(--text-muted);
}
.highlighted .select-menu-value,
.highlighted .select-menu-icon {
  color: var(--accent-hover);
}
.select-menu-popup {
  position: fixed;
  inset: auto;
  width: 268px;
  max-width: calc(100vw - 16px);
  margin: 0;
  padding: 6px;
  overflow: hidden;
  border: 1px solid var(--glass-edge-strong);
  border-radius: var(--radius-lg);
  background: var(--glass-sheen), var(--glass-float);
  -webkit-backdrop-filter: var(--glass-blur-strong);
  backdrop-filter: var(--glass-blur-strong);
  color: var(--text-secondary);
  box-shadow: var(--glass-highlight), var(--shadow-pop);
  font: var(--fs-md) / 1.5 var(--font-ui);
}
.select-menu-popup:popover-open {
  display: flex;
  flex-direction: column;
}
.select-menu-popup.searchable {
  width: 336px;
}
.select-menu-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex: none;
  padding: 6px 10px 8px;
  color: var(--text-faint);
  font-size: var(--fs-2xs);
  font-weight: 600;
  letter-spacing: 0.06em;
}
.select-menu-heading.has-action {
  padding: 2px 4px 6px 10px;
}
.select-menu-search {
  display: flex;
  align-items: center;
  flex: none;
  gap: 8px;
  margin: 0 4px 6px;
  padding: 0 10px;
  border: 1px solid var(--border-control);
  border-radius: var(--radius-sm);
  background: rgb(var(--well-rgb) / 0.42);
  color: var(--text-muted);
}
.select-menu-search:focus-within {
  border-color: rgb(var(--focus-rgb) / 0.6);
}
.select-menu-search > svg {
  flex: none;
}
.select-menu-popup .select-menu-search input {
  width: 100%;
  min-width: 0;
  min-height: 36px;
  margin: 0;
  padding: 7px 0;
  border: 0;
  border-radius: 0;
  outline: none;
  background: transparent;
  box-shadow: none;
  color: var(--text-primary);
  font: var(--fs-md) / 1.5 var(--font-ui);
}
.select-menu-list {
  min-height: 0;
  overflow: auto;
  overscroll-behavior: contain;
  scrollbar-width: thin;
  outline: none;
}
.select-menu-list:focus-visible {
  outline: 1px solid var(--accent);
  outline-offset: -1px;
  border-radius: var(--radius-xs);
}
.select-menu-option {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 38px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  cursor: pointer;
}
.select-menu-option.active {
  background: var(--hover-wash);
}
.select-menu-option[aria-selected='true'] {
  color: var(--accent-hover);
  background: var(--accent-soft);
}
.select-menu-option[aria-disabled='true'] {
  opacity: 0.45;
  cursor: not-allowed;
}
.select-menu-option-copy {
  min-width: 0;
  flex: 1;
  overflow-wrap: anywhere;
}
.select-menu-option-copy > span {
  display: block;
}
.select-menu-popup .select-menu-option-copy small {
  display: block;
  margin-top: 3px;
  color: var(--text-muted);
  font: var(--fs-xs) / 1.5 var(--font-ui);
}
.select-menu-check {
  flex: none;
}
.select-menu-option.custom {
  border-top: 1px solid var(--border-subtle);
  margin-top: 4px;
}
.select-menu-empty {
  padding: 20px 12px;
  color: var(--text-muted);
  text-align: center;
  font-size: var(--fs-sm);
}
.select-menu-error {
  padding: 8px 10px;
  color: var(--warning);
  font-size: var(--fs-sm);
  overflow-wrap: anywhere;
}
@container composer (max-width: 380px) {
  .compact .select-menu-caption,
  .compact .select-menu-icon {
    display: none;
  }
  .select-menu.compact .select-menu-trigger {
    padding: 6px 4px;
    gap: 4px;
  }
}
@media (pointer: coarse) {
  .select-menu .select-menu-trigger,
  .select-menu.compact .select-menu-trigger {
    min-height: 40px;
  }
  .select-menu-option {
    min-height: 44px;
  }
}
</style>
