import { describe, expect, it } from 'vitest';
import { adapterRule, updateAdapterRule, type MessageTypeDefinition } from '../../web/lib/messageAdapter';
import { difference } from '../../web/lib/settingsDocument';

const state: MessageTypeDefinition = {
  id: 'state',
  label: '剧情状态',
  tag: 'tavern_state',
  configurable: true,
  appendable: true,
  defaults: { chat_completions: { role: 'system', wrap: true }, anthropic: { role: 'user', wrap: true } },
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
});
