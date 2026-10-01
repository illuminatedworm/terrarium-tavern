import { describe, expect, it } from 'vitest';
import { splitCharacterBlock } from '../../web/lib/recipeBlocks';

describe('splitCharacterBlock', () => {
  it('preserves injection parameters and allocates unique IDs for old recipes', () => {
    const original = {
      id: 'character',
      source: 'character',
      role: 'developer',
      depth: 2,
      enabled: false,
      required: true,
      priority: 80,
      budget: 100,
      condition: 'ready',
      content: '',
    };
    const blocks = [original, { ...original, id: 'character_description' }];
    const split = splitCharacterBlock(original, blocks);
    expect(split.map((block) => block.source)).toEqual([
      'system_prompt',
      'description',
      'personality',
      'scenario',
    ]);
    expect(split[1].id).toBe('character_description_2');
    expect(new Set([...blocks, ...split].map((block) => block.id)).size).toBe(6);
    const { id: originalId, source: originalSource, ...expected } = original;
    for (const block of split) {
      const { id, source, ...parameters } = block;
      expect(parameters).toEqual(expected);
    }
    expect(originalId).toBe('character');
    expect(originalSource).toBe('character');
    expect(original.source).toBe('character');
  });
});
