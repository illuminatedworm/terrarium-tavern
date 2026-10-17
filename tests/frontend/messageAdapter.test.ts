import { describe, expect, it } from 'vitest';
import {
  adapterRule,
  resetAdapterRoles,
  updateAdapterRule,
  type MessageTypeDefinition,
} from '../../web/lib/messageAdapter';
import { difference } from '../../web/lib/settingsDocument';

const state: MessageTypeDefinition = {
  id: 'state',
  label: '剧情状态',
  tag: 'tavern_state',
  configurable: true,
  appendable: true,
  defaults: {
    chat_completions: { role: 'system', wrap: true },
    anthropic: { role: 'system', wrap: true },
    gemini: { role: 'user', wrap: true },
  },
};

describe('message adapter settings', () => {
  it('keeps protocol choices separate without changing the saved object', () => {
    const first = updateAdapterRule(undefined, 'chat_completions', state, { role: 'user', wrap: false });
    const second = updateAdapterRule(first, 'anthropic', state, { role: 'assistant' });
    expect(first.mappings.anthropic).toBeUndefined();
    expect(adapterRule(second, 'chat_completions', state)).toEqual({ role: 'user', wrap: false });
    expect(adapterRule(second, 'anthropic', state)).toEqual({ role: 'assistant', wrap: true });
  });

  it('saves adapter dictionaries atomically through the global settings API', () => {
    const before = { foreground: { message_adapter: { version: 1, mappings: {} } } };
    const after = {
      foreground: {
        message_adapter: updateAdapterRule(undefined, 'chat_completions', state, { role: 'user' }),
      },
    };
    expect(difference(before, after)).toEqual({
      'foreground.message_adapter': after.foreground.message_adapter,
    });
  });

  it('resets saved roles on an explicit protocol switch including injected lore and compatibility types', () => {
    const lore = { ...state, id: 'lore_injected', label: '中途／可变世界书' };
    const compatible = { ...lore, id: 'compat:lore_injected:assistant' };
    let saved = updateAdapterRule(undefined, 'gemini', lore, { role: 'system', wrap: false });
    saved = updateAdapterRule(saved, 'gemini', state, { role: 'assistant' });
    saved = updateAdapterRule(saved, 'gemini', compatible, { role: 'system' });
    saved = updateAdapterRule(saved, 'chat_completions', lore, { role: 'user' });
    const before = structuredClone(saved);
    const types = [state, lore, compatible];
    const switched = resetAdapterRoles(saved, 'gemini', types);
    expect(adapterRule(switched, 'gemini', lore)).toEqual({ role: 'user', wrap: false });
    expect(adapterRule(switched, 'gemini', state).role).toBe('user');
    expect(adapterRule(switched, 'gemini', compatible).role).toBe('user');
    expect(switched.mappings.chat_completions).toEqual(saved.mappings.chat_completions);
    const switchedBack = resetAdapterRoles(switched, 'chat_completions', types);
    expect(adapterRule(switchedBack, 'chat_completions', lore)).toEqual({ role: 'system', wrap: true });
    expect(switchedBack.mappings.gemini).toEqual(switched.mappings.gemini);
    expect(saved).toEqual(before);
  });
});
