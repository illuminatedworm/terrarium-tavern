/** API pages arrive newest first; every rendered page is oldest first. */
export function mergeWorkTurns(previous: any[], incoming: any[]) {
  const turns = new Map(previous.map((turn) => [turn.id, turn]));
  for (const turn of incoming) turns.set(turn.id, turn);
  return Array.from(turns.values()).sort((a, b) => a.created - b.created || a.id.localeCompare(b.id));
}
