export function maintenanceLimit(_settings: any): number {
  return 100;
}

export function settingsErrors(s: any): string[] {
  const errors: string[] = [];
  const integer = (key: string, label: string, min: number, max: number) => {
    const value = s[key];
    if (typeof value !== 'number' || !Number.isInteger(value) || value < min || value > max)
      errors.push(`${label}必须是 ${min}–${max} 的整数。`);
  };
  if (s.lore_recursion_depth !== undefined) integer('lore_recursion_depth', '世界书递归深度', 0, 16);
  if (s.lore_recursive !== undefined && typeof s.lore_recursive !== 'boolean')
    errors.push('世界书递归查询须为开关值。');
  if (s.lore_max_entries !== undefined) integer('lore_max_entries', '单次新增条目上限', 1, 1000);
  if (s.lore_max_chars !== undefined) integer('lore_max_chars', '单次新增字符预算', 1, 2000000);
  integer('context_tokens', '模型上下文窗口（tokens）', 2048, 2000000);
  integer('reply_tokens', '单次输出上限（tokens）', 128, 65536);
  integer('history_message_cap', '历史消息 cap', 8, 1000);
  integer('chunk_size', '每批移除量', 2, 100);
  integer('chunk_max_tokens', '历史预算（tokens）', 512, 2000000);
  integer('memory_timeout', '维护超时', 5, 180);
  integer('memory_interval_turns', '记忆周期', 1, 100);
  if (s.memory_current_max_records !== undefined)
    integer('memory_current_max_records', '当前进展条数上限', 1, 100);
  if (s.memory_episode_cooldown_rounds !== undefined)
    integer('memory_episode_cooldown_rounds', '经历冷却轮数', 0, 10000);
  for (const [key, label, min, max] of [
    ['memory_input_tokens', '记忆输入上限', 2048, 2000000],
    ['memory_evidence_tokens', '辅助资料预算', 0, 32768],
    ['memory_output_tokens', '记忆输出上限', 256, 65536],
    ['memory_capacity_tokens', '活跃记忆容量', 512, 65536],
  ] as const)
    if (s[key] !== undefined) integer(key, label, min, max);
  if (s.memory_evidence_tokens >= s.memory_input_tokens)
    errors.push('记忆辅助资料预算必须小于整理输入上限。');
  if (s.background && s.memory_output_tokens + 2048 >= s.background.context_tokens)
    errors.push('记忆输出上限必须为后台模型输入及安全余量留出空间。');
  if (s.daily_context_budget !== undefined)
    integer('daily_context_budget', '日常上下文预算（tokens）', 2048, 2000000);
  if (s.log_retention_days !== undefined) integer('log_retention_days', '日志保留天数', 1, 365);
  for (const key of ['log_max_mib', 'retrieval_cache_max_mib'])
    if (s[key] !== undefined) integer(key, '存储限额', 1, 16384);
  if (Number(s.context_tokens) <= Number(s.reply_tokens) + 2048)
    errors.push('模型上下文窗口必须大于单次输出上限/维护输出预算加 2048 tokens 的安全余量。');
  const trigger = s.context_pressure_trigger,
    target = s.context_pressure_target;
  if (typeof trigger !== 'number' || !Number.isFinite(trigger) || trigger < 0.1 || trigger > 0.99)
    errors.push('压力触发阈值须为 10%–99%。');
  if (typeof target !== 'number' || !Number.isFinite(target) || target < 0.05 || target > 0.98)
    errors.push('压力裁剪目标须为 5%–98%。');
  if (target >= trigger) errors.push('压力裁剪目标必须小于压力触发阈值。');
  if (s.chunk_mode === 'tokens' && s.chunk_max_tokens > s.context_tokens - s.reply_tokens - 2048)
    errors.push('历史预算不能超过模型上下文窗口减单次输出上限及 2048 tokens 安全余量。');
  if (s.retrieval && !s.retrieval.channels.length) errors.push('至少选择一种召回方案。');
  if (s.retrieval && s.retrieval.chunk_overlap >= s.retrieval.chunk_tokens)
    errors.push('切片重叠必须小于切片长度。');
  for (const role of ['foreground', 'background', 'card_editor', 'full_agent']) {
    const profile = s[role];
    if (profile && profile.context_tokens <= profile.reply_tokens + 2048)
      errors.push(
        `${role === 'foreground' ? '前台 Writer' : role === 'card_editor' ? 'Card Editor' : role === 'full_agent' ? 'Full Agent' : '后台 Tracker'}模型上下文窗口必须大于输出预算加 2048 tokens。`,
      );
    if (profile && !s.providers?.some((p: any) => p.id === profile.provider_id))
      errors.push('所选提供商不存在。');
  }
  return errors;
}
