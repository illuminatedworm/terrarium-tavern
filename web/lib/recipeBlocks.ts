export function splitCharacterBlock<T extends { id: string; source: string }>(block: T, blocks: T[]): T[] {
  const ids = new Set(blocks.map((item) => item.id));
  return ['system_prompt', 'description', 'personality', 'scenario'].map((source) => {
    let id = `${block.id}_${source}`;
    for (let suffix = 2; ids.has(id); suffix++) id = `${block.id}_${source}_${suffix}`;
    ids.add(id);
    return { ...block, id, source };
  });
}
