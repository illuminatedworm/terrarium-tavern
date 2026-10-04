import { describe, it, expect } from 'vitest';
import { apply, commands, initialize } from '../../compat/mvu';
describe('MVU transaction semantics', () => {
  const emit = async () => {};
  it('replaces worldbook defaults with the selected opening and preserves subsequent progress', async () => {
    const entries = [
      {
        id: 'default',
        comment: '[InitVar]',
        content: 'world: default\nhp: 500\nextra: true\nabilities: {sense: true}\nitems: [a, b]',
      },
    ];
    const greeting =
      'Story\r\n<UpdateVariable>\r\n<initvar>world: alternate\r\nhp: 100\r\nabilities: {magic: true}\r\nitems: []</initvar></UpdateVariable>';
    const first = await initialize(entries, {}, emit, greeting);
    expect(first.stat_data).toEqual({ world: 'alternate', hp: 100, abilities: { magic: true }, items: [] });
    first.stat_data.hp = 80;
    expect((await initialize(entries, first, emit, greeting)).stat_data.hp).toBe(80);
    const reopened = await initialize(
      [...entries, { id: 'new-default', comment: '[initvar]', content: 'extra: true' }],
      first,
      emit,
    );
    expect(reopened.stat_data).toEqual(first.stat_data);
    expect(reopened.schema.properties).not.toHaveProperty('extra');
  });
  it('reads disabled InitVar entries while excluding ordinary lore and Tracker rules', async () => {
    const data = await initialize(
      [
        { id: 'off', comment: '[INITVAR]初始化', enabled: false, content: 'hp: 100' },
        { id: 'legacy-off', comment: '[initvar]', disable: true, content: 'mp: 50' },
        { id: 'story', comment: '普通设定', enabled: false, content: 'extra: true' },
        {
          id: 'rule',
          comment: '[initvar]',
          extensions: { terrarium: { audience: 'tracker_mvu' } },
          content: 'secret: true',
        },
      ],
      {},
      emit,
    );
    expect(data.stat_data).toEqual({ hp: 100, mp: 50 });
    expect(Object.keys(data.initialized_lorebooks)).toEqual(['card:off', 'card:legacy-off']);
  });
  it('combines same-worldbook fragments with later arrays replacing earlier arrays', async () => {
    const data = await initialize(
      [
        { id: 'one', comment: '[initvar]', content: 'player: {hp: 10, items: [a, b]}' },
        { id: 'two', comment: '[initvar]', content: 'player: {mp: 5, items: [c]}' },
      ],
      {},
      emit,
    );
    expect(data.stat_data).toEqual({ player: { hp: 10, mp: 5, items: ['c'] } });
  });
  it('preserves existing root objects without restoring missing child defaults', async () => {
    const old = { stat_data: { player: { hp: 7, items: [] } } };
    const data = await initialize(
      [
        {
          id: 'default',
          comment: '[initvar]',
          content: 'player: {hp: 10, mp: 5, items: [a, b]}\nworld: town',
        },
      ],
      old,
      emit,
    );
    expect(data.stat_data).toEqual({ player: { hp: 7, items: [] }, world: 'town' });
    expect(old).toEqual({ stat_data: { player: { hp: 7, items: [] } } });
  });
  it('combines opening blocks in order and ignores invalid worldbook defaults', async () => {
    const data = await initialize(
      [{ id: 'ignored', comment: '[initvar]', content: 'this is not an object' }],
      {},
      emit,
      '<initvar>player: {hp: 10, items: [a, b]}</initvar>\n<initvar>```yaml\nplayer: {hp: 8, mp: 5, items: [c]}\n```</initvar>',
    );
    expect(data.stat_data).toEqual({ player: { hp: 8, mp: 5, items: ['c'] } });
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
