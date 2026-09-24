import { describe, expect, it } from 'vitest';
import { maintenanceLimit, settingsErrors } from '../../web/lib/settingsValidation';

const defaults = {
  lore_max_entries: 5,
  lore_max_chars: 3000,
  context_tokens: 32768,
  reply_tokens: 2048,
  history_message_cap: 40,
  chunk_size: 10,
  chunk_mode: 'messages',
  chunk_max_tokens: 6000,
  memory_timeout: 120,
  memory_interval_turns: 5,
  memory_current_max_records: 5,
  memory_episode_cooldown_rounds: 20,
  context_pressure_trigger: 0.85,
  context_pressure_target: 0.7,
};

describe('linked global settings', () => {
  it('accepts defaults and calculates the shared bound', () => {
    expect(settingsErrors(defaults)).toEqual([]);
    expect(maintenanceLimit(defaults)).toBe(100);
    expect(maintenanceLimit({ ...defaults, history_message_cap: 18 })).toBe(100);
  });
  it.each([
    { lore_max_entries: 0 },
    { lore_max_chars: 0 },
    { lore_max_chars: 1.5 },
    { history_message_cap: 7 },
    { history_message_cap: 1001 },
    { chunk_size: 101 },
    { daily_context_budget: 1 },
    { memory_interval_turns: 101 },
    { memory_current_max_records: 0 },
    { memory_current_max_records: 101 },
    { memory_episode_cooldown_rounds: -1 },
    { memory_episode_cooldown_rounds: 10001 },
    { memory_interval_turns: 101 },
    { context_tokens: 4096 },
    { context_pressure_target: 0.85 },
    { context_pressure_trigger: 0.05 },
    { chunk_mode: 'tokens', chunk_max_tokens: 29000 },
    { memory_timeout: 181 },
    { memory_input_tokens: 2047 },
    { memory_evidence_tokens: 4096, memory_input_tokens: 4096 },
    { memory_output_tokens: 255 },
    { memory_capacity_tokens: 511 },
    { background: { context_tokens: 8192, reply_tokens: 9000 } },
  ])('rejects conflicting configuration %j without modifying fields', (changes) => {
    const value = { ...defaults, ...changes },
      before = { ...value };
    expect(settingsErrors(value).length).toBeGreaterThan(0);
    expect(value).toEqual(before);
  });
  it('accepts safe minimum and suspends cap linkage in off mode', () => {
    expect(
      settingsErrors({
        ...defaults,
        history_message_cap: 8,
        chunk_size: 2,
        memory_interval_turns: 1,
      }),
    ).toEqual([]);
    expect(settingsErrors({ ...defaults, chunk_mode: 'off', chunk_size: 3 })).toEqual([]);
    expect(maintenanceLimit({ ...defaults, chunk_mode: 'off' })).toBe(100);
  });
});

describe('worldbook recursion settings', () => {
  it('accepts disabled recursion and zero depth', () => {
    expect(settingsErrors({ ...defaults, lore_recursive: false, lore_recursion_depth: 0 })).toEqual([]);
    expect(settingsErrors({ ...defaults, lore_recursive: true, lore_recursion_depth: 16 })).toEqual([]);
  });
  it.each([
    { lore_recursion_depth: -1 },
    { lore_recursion_depth: 17 },
    { lore_recursion_depth: 1.5 },
    { lore_recursive: 'false' },
  ])('rejects invalid values: %o', (values) => {
    expect(settingsErrors({ ...defaults, ...values }).length).toBeGreaterThan(0);
  });
});
