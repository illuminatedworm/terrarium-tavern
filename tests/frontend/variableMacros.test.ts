import { describe, expect, it } from 'vitest';
import { snapshotMacros } from '../../compat/variable-macros';

const state = {
  stat_data: {
    hp: [45, '当前生命'],
    items: ['钥匙', '地图'],
    money: 0,
    alive: false,
    missing: null,
    details: { text: '完整文本', $hidden: '不能显示' },
    'a.b': 7,
  },
};

describe('Tavern Helper variable macro semantics', () => {
  it('reads scalar values and explicit pair indexes without flattening real arrays', () => {
    const text =
      '{{format_message_variable::stat_data.hp.0}}|{{get_message_variable::stat_data.hp}}|{{get_chat_variable::stat_data.items}}|{{get_message_variable::stat_data.money}}|{{format_chat_variable::stat_data.alive}}';
    expect(snapshotMacros(text, state)).toBe('45|[45,"当前生命"]|["钥匙","地图"]|0|false');
    expect(state.stat_data.hp).toEqual([45, '当前生命']);
  });
  it('handles bracket paths, escaped path quotes, scope isolation and missing values', () => {
    expect(
      snapshotMacros(
        '{{get_message_variable::stat_data[&quot;a.b&quot;]}}|{{get_global_variable::score}}|{{get_character_variable::score}}|{{get_preset_variable::score}}',
        state,
        { global: { score: 1 }, character: { score: 2 }, preset: { score: 3 } },
      ),
    ).toBe('7|1|2|3');
    expect(
      snapshotMacros(
        '{{format_message_variable::stat_data.absent}}|{{get_message_variable::stat_data.missing}}',
        state,
      ),
    ).toBe('null|null');
  });
  it('hides dollar-prefixed fields even when directly addressed, while retaining unknown macros', () => {
    expect(
      snapshotMacros(
        '{{get_message_variable::stat_data.details}}|{{get_message_variable::stat_data.details.$hidden}}|{{unknown}}',
        state,
      ),
    ).toBe('{"text":"完整文本"}|null|{{unknown}}');
    expect(snapshotMacros('{{get_message_variable::constructor}}|{{get_message_variable::}}', state)).toBe(
      'null|null',
    );
  });
  it('indents complete YAML beneath the prefix and supports quoted values', () => {
    expect(snapshotMacros('物品：{{format_message_variable::stat_data.items}}', state)).toBe(
      '物品：- 钥匙\n   - 地图',
    );
    expect(snapshotMacros('{{format_message_variable_quoted::stat_data.details}}', state)).toBe(
      'text: "完整文本"',
    );
  });
});
