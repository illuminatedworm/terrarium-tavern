/**
 * Preview-only local tokenizer reference (`local_estimate`). It compares the
 * relative size of blocks and drives no window, compaction or warning logic;
 * the provider observation in `context_usage` stays the only measured value.
 */
export interface LocalEstimate {
  unit: 'tokens';
  tokens: number;
  tool_definitions: number;
  tokenizer: string;
  tokenizer_source: string;
  image_blocks: number;
  encrypted_blocks: number;
}

interface EstimatedRow {
  local_tokens?: number | null;
  local_excluded?: string[];
}

export function localTokensLabel(row?: EstimatedRow | null): string | undefined {
  if (!row || row.local_tokens === undefined) return undefined;
  if (row.local_tokens == null) return '加密状态 · 不计入';
  return `≈ ${row.local_tokens.toLocaleString()} tokens${row.local_excluded?.includes('image') ? ' · 图片不计入' : ''}`;
}

/** Sum the per-block reference by category, plus tool definitions. */
export function localEstimateSources(
  rows: EstimatedRow[],
  estimate: LocalEstimate | null | undefined,
  keyFor: (row: any, index: number) => string,
): Record<string, number> {
  const totals: Record<string, number> = {};
  if (!estimate) return totals;
  rows.forEach((row, index) => {
    if (row.local_tokens == null) return;
    const key = keyFor(row, index);
    totals[key] = (totals[key] ?? 0) + row.local_tokens;
  });
  if (estimate.tool_definitions) totals.tool_definitions = estimate.tool_definitions;
  return totals;
}

export function localEstimateLabel(estimate?: LocalEstimate | null): string {
  if (!estimate) return '';
  return `本地分词预测 ≈ ${estimate.tokens.toLocaleString()} tokens · 仅供比较相对大小`;
}

export function localEstimateNote(estimate?: LocalEstimate | null): string {
  if (!estimate) return '';
  const excluded = [
    estimate.encrypted_blocks ? `密文 ${estimate.encrypted_blocks} 块` : '',
    estimate.image_blocks ? `含图片 ${estimate.image_blocks} 块` : '',
  ].filter(Boolean);
  return (
    `本地分词预测使用 ${estimate.tokenizer}${estimate.tokenizer_source === 'reference_fallback' ? '（未知模型，参考分词器）' : ''}，` +
    '只用于比较各块相对大小，不是接口实测，也不参与压力、换窗、整理或警告；密文与图片不计入' +
    (excluded.length ? `（本次${excluded.join('、')}）` : '') +
    '。'
  );
}
