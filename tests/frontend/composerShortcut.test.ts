import { describe, expect, it, vi } from 'vitest';
import { handleComposerKeydown, sendShortcutHint, type SendShortcut } from '../../web/lib/composerShortcut';

function key(overrides: Partial<KeyboardEvent> = {}) {
  return {
    key: 'Enter',
    shiftKey: false,
    ctrlKey: false,
    metaKey: false,
    altKey: false,
    isComposing: false,
    repeat: false,
    keyCode: 13,
    preventDefault: vi.fn(),
    ...overrides,
  } as unknown as KeyboardEvent;
}

describe('chat composer shortcuts', () => {
  it('keeps Enter as a newline and sends on Ctrl+Enter by default', () => {
    const newline = key();
    expect(handleComposerKeydown(newline)).toBe(false);
    expect(newline.preventDefault).not.toHaveBeenCalled();
    const send = key({ ctrlKey: true });
    expect(handleComposerKeydown(send)).toBe(true);
    expect(send.preventDefault).toHaveBeenCalledOnce();
    expect(sendShortcutHint()).toBe(sendShortcutHint('ctrl_enter'));
  });

  it.each<[SendShortcut, Partial<KeyboardEvent>]>([
    ['enter', {}],
    ['ctrl_enter', { ctrlKey: true }],
    ['ctrl_enter', { metaKey: true }],
    ['shift_enter', { shiftKey: true }],
  ])('sends only on the configured %s chord', (mode, modifiers) => {
    const event = key(modifiers);
    expect(handleComposerKeydown(event, mode)).toBe(true);
    expect(event.preventDefault).toHaveBeenCalledOnce();
  });

  it.each<[SendShortcut, Partial<KeyboardEvent>]>([
    ['enter', { shiftKey: true }],
    ['enter', { ctrlKey: true }],
    ['ctrl_enter', {}],
    ['ctrl_enter', { ctrlKey: true, shiftKey: true }],
    ['shift_enter', {}],
    ['shift_enter', { ctrlKey: true, shiftKey: true }],
    ['button_only', {}],
    ['button_only', { ctrlKey: true }],
    ['button_only', { shiftKey: true }],
    ['enter', { altKey: true }],
    ['enter', { key: 'a' }],
  ])('preserves native input for %s with %j', (mode, modifiers) => {
    const event = key(modifiers);
    expect(handleComposerKeydown(event, mode)).toBe(false);
    expect(event.preventDefault).not.toHaveBeenCalled();
  });

  it.each(['enter', 'ctrl_enter', 'shift_enter'] as const)(
    'does not send during IME or key repeat: %s',
    (mode) => {
      const modifiers = { ctrlKey: mode === 'ctrl_enter', shiftKey: mode === 'shift_enter' };
      for (const composition of [{ isComposing: true }, { keyCode: 229 }]) {
        const event = key({ ...modifiers, ...composition });
        expect(handleComposerKeydown(event, mode)).toBe(false);
        expect(event.preventDefault).not.toHaveBeenCalled();
      }
      const held = key({ ...modifiers, repeat: true });
      expect(handleComposerKeydown(held, mode)).toBe(false);
      expect(held.preventDefault).toHaveBeenCalledOnce();
    },
  );
});
