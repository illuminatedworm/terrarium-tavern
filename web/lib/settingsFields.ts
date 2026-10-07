import type { SettingField } from '../components/settings/SettingsFields.vue';
const number = (
  path: string,
  label: string,
  min: number,
  max: number,
  step: number | string = 1,
): SettingField => ({ path, label, min, max, step });
export const policies = [
  { value: 'directed', label: '设定需授权，状态自动保存' },
  { value: 'evolving', label: '允许修改设定，自动保存' },
  { value: 'preview', label: '状态与设定更新均需确认' },
];
export const policyField: SettingField[] = [
  {
    path: 'policy',
    label: '默认对话更新策略',
    options: policies,
    help: '未单独设置权限的同模式对话，从下一轮起采用此策略。',
  },
];
export const loreFields: SettingField[] = [
  number('lore_max_entries', '单次新增条目上限', 1, 1000),
  { path: 'lore_recursive', label: '世界书递归查询', type: 'checkbox' },
  number('lore_recursion_depth', '递归深度', 0, 16),
];
export const retrievalFields: SettingField[] = [
  number('retrieval.bm25_min_coverage', '关键词最低覆盖率', 0, 1, 0.05),
  number('retrieval.bm25_min_score', '关键词最低相对得分', 0, 1, 0.05),
  number('retrieval.embedding_min_similarity', '向量最低相似度', -1, 1, 0.05),
  number('retrieval.rerank_min_score', '重排最低相关性', 0, 1, 0.05),
  { path: 'local_runtime.reranker_enabled', label: '启用本地重排', type: 'checkbox' },
  number('local_runtime.rerank_fragments', '每条重排片段数', 1, 8),
];
export const retrievalAdvanced: SettingField[] = [
  number('retrieval.candidate_count', '每通道候选数', 1, 1000),
  number('retrieval.fusion_count', '融合候选数', 1, 1000),
  number('retrieval.rrf_k', '排名融合平滑系数', 1, 1000),
  number('retrieval.bm25_k1', 'BM25 k1', 0.01, 10, 0.1),
  number('retrieval.bm25_b', 'BM25 b', 0, 1, 0.05),
  number('retrieval.gram_weight', '字符二元组权重', 0, 1, 0.05),
  number('retrieval.query_lookback', '线索回看数量', 0, 20),
  number('retrieval.query_chars', '线索超长提示阈值（字符）', 32, 4096),
  number('retrieval.chunk_tokens', '切片长度（分词单位）', 64, 4096),
  number('retrieval.chunk_overlap', '切片重叠（分词单位）', 0, 1024),
  number('retrieval.embedding_timeout', '向量查询超时（秒）', 0.1, 120, 0.1),
  number('retrieval.rerank_timeout', '重排超时（秒）', 0.1, 120, 0.1),
];
export const fullFields: SettingField[] = [
  { path: 'tools.auto_retrieval', label: '每轮自动检索世界书', type: 'checkbox' },
  number('tools.max_steps', '每回合模型调用上限', 2, 100),
  {
    ...number('tools.pressure_ratio', '上下文占用提醒阈值（比例）', 0.25, 0.9, 0.05),
    help: '上下文估算 token 数达到窗口容量的此比例时，提醒模型准备换窗。0.7 表示 70%；提醒本身不强制换窗。',
  },
  number('tools.handoff_tokens', '交接笔记上限（tokens）', 128, 16384),
  number('tools.note_tokens', '单条长期笔记上限（tokens）', 128, 16384),
];
export const historyFields: SettingField[] = [
  {
    path: 'chunk_mode',
    label: '窗口模式',
    options: [
      { value: 'messages', label: '按消息批次' },
      { value: 'tokens', label: '按历史预算' },
      { value: 'off', label: '关闭，保留全部原文' },
    ],
  },
  number('history_message_cap', '历史消息上限', 8, 1000),
  number('chunk_size', '每批移除量', 2, 100),
  number('memory_interval_turns', '记忆整理周期（轮）', 1, 100),
  number('daily_context_budget', '日常上下文预算（tokens）', 2048, 2000000),
  number('chunk_max_tokens', '历史预算（tokens）', 512, 2000000),
  number('context_pressure_trigger', '压力触发比例', 0.1, 0.99, 0.01),
  number('context_pressure_target', '压力裁剪目标比例', 0.05, 0.98, 0.01),
];
export const memoryFields: SettingField[] = [
  number('tracker_max_steps', 'Tracker 每次任务模型调用上限', 2, 100),
  number('memory_input_tokens', '整理输入上限（tokens）', 2048, 2000000),
  number('memory_output_tokens', '整理输出上限（tokens）', 256, 65536),
  number('memory_evidence_tokens', '辅助资料（0 关闭，其余不限制长度）', 0, 32768),
  number('memory_capacity_tokens', '活跃记忆容量（tokens）', 512, 65536),
  number('memory_current_max_records', '当前进展条数上限', 1, 100),
  number('memory_episode_cooldown_rounds', '经历冷却轮数', 0, 10000),
  number('memory_timeout', '每次维护超时（秒）', 5, 180),
  { path: 'memory_review_enabled', label: '允许一次疑难复核', type: 'checkbox' },
];
export const networkFields: SettingField[] = [
  { path: 'network.proxy', label: '代理地址', type: 'text' },
  { path: 'network.api_proxy', label: '模型接口请求使用代理', type: 'checkbox' },
  { path: 'network.hf_proxy', label: '本地模型下载使用代理', type: 'checkbox' },
  { path: 'network.resources_proxy', label: '角色外部资源使用代理', type: 'checkbox' },
];
