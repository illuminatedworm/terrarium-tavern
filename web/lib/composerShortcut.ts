export type SendShortcut = 'enter' | 'ctrl_enter' | 'shift_enter' | 'button_only';

export const sendShortcutOptions: Array<{ value: SendShortcut; label: string }> = [
  { value: 'enter', label: 'Enter 发送 · Shift+Enter 换行' },
  { value: 'ctrl_enter', label: 'Ctrl+Enter 发送 · Enter 换行' },
  { value: 'shift_enter', label: 'Shift+Enter 发送 · Enter 换行' },
  { value: 'button_only', label: '仅点击按钮发送 · Enter 换行' },
];

export function sendShortcutHint(shortcut: SendShortcut = 'ctrl_enter'): string {
  return sendShortcutOptions.find((option) => option.value === shortcut)!.label;
}

/** Only consume the configured chord; IME confirmation and held keys never send. */
export function handleComposerKeydown(event: KeyboardEvent, shortcut: SendShortcut = 'ctrl_enter'): boolean {
  if (event.key !== 'Enter' || event.isComposing || event.keyCode === 229 || event.altKey) return false;
  const matches =
    shortcut === 'enter'
      ? !event.shiftKey && !event.ctrlKey && !event.metaKey
      : shortcut === 'ctrl_enter'
        ? !event.shiftKey && (event.ctrlKey || event.metaKey)
        : shortcut === 'shift_enter'
          ? event.shiftKey && !event.ctrlKey && !event.metaKey
          : false;
  if (!matches) return false;
  event.preventDefault();
  return !event.repeat;
}
