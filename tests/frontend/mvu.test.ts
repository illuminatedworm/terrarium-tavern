import { describe, it, expect } from 'vitest';
import { apply, commands, initialize } from '../../compat/mvu';
describe('MVU transaction semantics', () => {
  const emit = async () => {};
  it('uses the selected opening before defaults and never resets subsequent progress', async () => {
    const entries = [
      { id: 'default', comment: '[InitVar]', content: 'world: default\nhp: 500\nextra: true' },
    ];
    const greeting =
      'Story\r\n<UpdateVariable>\r\n<initvar>world: alternate\r\nhp: 100</initvar></UpdateVariable>';
    const first = await initialize(entries, {}, emit, greeting);
    expect(first.stat_data).toEqual({ world: 'alternate', hp: 100, extra: true });
    first.stat_data.hp = 80;
    expect((await initialize(entries, first, emit, greeting)).stat_data.hp).toBe(80);
  });
  it('initializes once and preserves values', async () => {
    const entry = { id: '1', comment: '[InitVar]', content: '能量: 5' };
    const first = await initialize([entry], {}, emit);
    first.stat_data.能量 = 3;
    expect((await initialize([entry], first, emit)).stat_data.能量).toBe(3);
  });
  it('applies commands and lets callbacks correct values', async () => {
    const old = { stat_data: { 能量: 2 } };
    const result = await apply("_.add('能量', -4);", old, async (name, data) => {
      if (name === 'mag_variable_update_ended') data.stat_data.能量 = Math.max(0, data.stat_data.能量);
    });
    expect(result.data.stat_data.能量).toBe(0);
    expect(old.stat_data.能量).toBe(2);
  });
  it('rejects executable expressions and prototype paths', async () => {
    await expect(apply("_.set('x', fetch('https://bad'));", { stat_data: {} }, emit)).rejects.toThrow();
    await expect(apply("_.set('__proto__.x', 1);", { stat_data: {} }, emit)).rejects.toThrow();
  });
  it('supports described values, nested literals and JSON Patch', async () => {
    const old = { stat_data: { 能量: [2, '剩余能量'], list: [] } };
    const result = await apply(
      `_.add('能量', 3); _.insert('list', {'name':'a(b)'}); <JSONPatch>[{"op":"add","path":"/位置","value":"窗边"}]</JSONPatch>`,
      old,
      emit,
    );
    expect(result.data.stat_data).toEqual({ 能量: [5, '剩余能量'], list: [{ name: 'a(b)' }], 位置: '窗边' });
  });
  it('stops on stale expected value', async () => {
    await expect(apply("_.set('x', 2, 3);", { stat_data: { x: 1 } }, emit)).rejects.toThrow('旧值');
  });
});
