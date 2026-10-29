export interface ContextUsage {
  tokens: number | null;
  source: 'provider' | 'unavailable';
  coverage: 'exact' | 'appended' | 'changed' | 'unavailable';
  added_blocks: number | null;
  pending: boolean;
  observed_at: number | null;
  unavailable_reason: string | null;
  input_scope: 'request' | 'last_iteration' | null;
  output_tokens: number | null;
  thinking_tokens: number | null;
  output_includes_thinking: boolean;
  reported_total_tokens: number | null;
  cached_input_tokens: number | null;
  cache_write_input_tokens: number | null;
  input_modalities: Array<{ modality: string; tokens: number }> | null;
  output_modalities: Array<{ modality: string; tokens: number }> | null;
  candidate_tokens: number | null;
  tool_prompt_tokens: number | null;
  reasoning_context: string | null;
  context_edits: Array<{
    type: string;
    cleared_input_tokens?: number;
    cleared_thinking_turns?: number;
    cleared_tool_uses?: number;
  }> | null;
  input_transformations: Record<string, number> | null;
  compacted: boolean;
  model: string | null;
  /** The provider rejected the matching previous request as over its window. */
  overflow?: boolean;
  /** Latest attempt, distinct from the completed observation retained after failure. */
  last_attempt?:
    | 'pending'
    | 'completed'
    | 'request_failed'
    | 'request_cancelled'
    | 'interrupted'
    | 'context_overflow'
    | null;
}

interface ContextStats {
  context_usage?: ContextUsage;
}

export function contextTokens(stats?: ContextStats | null): number | null {
  // Archived previews can still contain the pre-migration local/calibrated
  // shape. Those estimates must never acquire a provider-measurement label.
  const usage = stats?.context_usage;
  return usage?.source === 'provider' ? usage.tokens : null;
}

function attemptFailure(usage?: ContextUsage): string | undefined {
  switch (usage?.last_attempt) {
    case 'request_failed':
      return '最近请求失败';
    case 'request_cancelled':
      return '最近请求已取消';
    case 'interrupted':
      return '最近请求因重启中断';
  }
}

export function contextUsageLabel(stats?: ContextStats | null): string {
  const usage = stats?.context_usage;
  const failure = attemptFailure(usage);
  if (contextTokens(stats) == null) {
    const label = usage?.pending
      ? '等待实测'
      : usage?.overflow
        ? `${failure ? '此前' : '上次'}请求超出窗口`
        : '暂无实测';
    return failure ? `${label} · ${failure}` : label;
  }
  const when = failure ? '此前' : '上次';
  const label = usage?.input_scope === 'last_iteration' ? `${when}主模型采样实测输入` : `${when}请求实测输入`;
  const state = usage?.pending
    ? '请求中'
    : usage?.coverage === 'changed'
      ? '仅供历史参考'
      : usage?.coverage === 'appended'
        ? '后续未计量'
        : '';
  return [label, failure, state].filter(Boolean).join(' · ');
}

const modalityLabels: Record<string, string> = {
  TEXT: '文本',
  IMAGE: '图片',
  AUDIO: '音频',
  VIDEO: '视频',
};

export function contextUsageRows(stats?: ContextStats | null): Array<{ label: string; value: string }> {
  const usage = stats?.context_usage;
  if (!usage) return [];
  const rows: Array<{ label: string; value: string }> = [];
  const add = (label: string, value: number | null | undefined) => {
    if (value != null) rows.push({ label, value: value.toLocaleString() + ' tokens' });
  };
  add(
    usage.output_includes_thinking ? '该次响应输出（含思考）' : '该次候选输出（不含思考）',
    usage.output_tokens,
  );
  add('该次响应内部思考', usage.thinking_tokens);
  add('接口报告总用量', usage.reported_total_tokens);
  add('实测输入中的缓存读取', usage.cached_input_tokens);
  add('实测输入中的缓存写入', usage.cache_write_input_tokens);
  if (usage.candidate_tokens !== usage.output_tokens) add('选中候选输出', usage.candidate_tokens);
  add('服务端工具输入', usage.tool_prompt_tokens);
  for (const [prefix, parts] of [
    ['输入', usage.input_modalities],
    ['输出', usage.output_modalities],
  ] as const)
    for (const part of parts ?? [])
      add(prefix + (modalityLabels[part.modality] || part.modality), part.tokens);
  if (usage.input_scope === 'last_iteration')
    rows.push({ label: '输入观测口径', value: '最后一次主模型采样；输出为该次响应总量' });
  if (usage.reasoning_context)
    rows.push({
      label: '接口返回的思考范围',
      value:
        usage.reasoning_context === 'current_turn'
          ? '仅当前轮'
          : usage.reasoning_context === 'all_turns'
            ? '可用历史思考'
            : usage.reasoning_context,
    });
  if (usage.context_edits != null) {
    rows.push({ label: '接口报告的上下文清除', value: usage.context_edits.length + ' 项' });
    for (const [index, edit] of usage.context_edits.entries())
      add('第 ' + (index + 1) + ' 项清除的输入', edit.cleared_input_tokens);
  }
  if (usage.input_transformations != null)
    rows.push({
      label: '接口报告的输入调整',
      value: Object.values(usage.input_transformations).reduce((sum, count) => sum + count, 0) + ' 项',
    });
  if (usage.observed_at != null)
    rows.push({ label: '观测时间', value: new Date(usage.observed_at * 1000).toLocaleString() });
  return rows;
}

/** Pass `rows: false` where the same rows are already shown as meter stats. */
export function contextUsageDetails(stats?: ContextStats | null, { rows = true } = {}): string {
  const usage = stats?.context_usage;
  if (!usage) return '尚无接口实测快照；新增内容等待下次请求计量。';
  const details = [contextUsageLabel(stats)];
  const tokens = contextTokens(stats);
  if (tokens != null) details[0] += ' ' + tokens.toLocaleString() + ' tokens';
  if (usage.pending)
    details.push(usage.tokens == null ? '请求进行中，等待接口反馈' : '请求进行中，显示此前快照');
  if (attemptFailure(usage) && tokens != null)
    details.push('最新尝试没有取得新实测，当前显示此前已完成请求的记录');
  if (usage.coverage === 'appended')
    details.push('此后新增 ' + usage.added_blocks + ' 个内容块，尚未作为输入计量');
  if (usage.coverage === 'changed')
    details.push('内容已改写、删除或重排；该快照仅供历史参考，不触发自动整理');
  if (usage.source === 'unavailable' && !usage.pending)
    details.push(
      usage.overflow
        ? `${attemptFailure(usage) ? '此前请求曾被' : '上次请求被'}服务端明确拒绝超出上下文窗口；未取得计数，下一次自动整理或窗口退出依此触发`
        : usage.unavailable_reason === 'source_changed'
          ? '当前来源或窗口尚无有效实测'
          : '未取得有效输入计数，不以本地估值或较早请求补齐',
    );
  if (usage.compacted) details.push('该次响应含云端压缩状态；压缩后的占用等待新实测');
  if (rows) details.push(...contextUsageRows(stats).map((row) => row.label + ' ' + row.value));
  details.push('输出、思考和总用量不代表当前本地上下文长度');
  return details.join('；') + '。';
}
