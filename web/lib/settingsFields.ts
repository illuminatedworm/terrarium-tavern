import type { SettingField } from '../components/settings/SettingsFields.vue';
const number = (
  path: string,
  label: string,
  min: number,
  max: number,
  step: number | string = 1,
): SettingField => ({ path, label, min, max, step });
export const policies = [
  {
    value: 'automatic',
    label: '自动保存',
    description: '变量、角色设定和世界书的修改随本轮正文一起保存。',
  },
  {
    value: 'review',
    label: '审核后保存',
    description: '变量、角色设定或世界书实际改变时，先展示修改；接受后整轮保存，拒绝则整轮不提交。',
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
  {
    path: 'tools.parallel_tool_calls',
    label: '单次允许多个工具调用',
    type: 'checkbox',
    help: '合并独立操作，减少等待。关闭后每次只调用一个工具。',
  },
  number('tools.max_steps', '每回合模型调用上限', 2, 100),
  {
    ...number('tools.pressure_ratio', '上下文占用提醒阈值（比例）', 0.25, 0.9, 0.05),
    help: '上下文估算 token 数达到窗口容量的此比例时，提醒模型准备换窗。0.7 表示 70%；提醒本身不强制换窗。',
  },
  number('tools.handoff_tokens', '交接笔记上限（tokens）', 128, 16384),
  number('tools.note_tokens', '单条长期笔记上限（tokens）', 128, 16384),
  number('tools.plot_tokens', '剧情规划上限（tokens）', 128, 16384),
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
  {
    path: 'model_request_mode',
    label: '模型请求方式',
    options: [
      { value: 'auto', label: '保持现有行为' },
      { value: 'stream', label: '流式' },
      { value: 'non_stream', label: '非流式' },
    ],
    help: '统一控制普通 API 的正文、后台和制卡请求，保存后从下一次操作生效。保持现有行为时由各任务选择。Full Agent 流式正文先显示为未保存预览，完整校验通过后才正式保存。ChatGPT 订阅通道始终使用流式。',
  },
  { path: 'network.proxy', label: '代理地址', type: 'text' },
  {
    path: 'network.api_proxy',
    label: '模型接口请求使用代理',
    type: 'checkbox',
    help: '同时控制本地搜索和网页读取；本地接入不受提供商代理开关影响。',
  },
  { path: 'network.hf_proxy', label: '本地模型下载使用代理', type: 'checkbox' },
  { path: 'network.resources_proxy', label: '角色外部资源使用代理', type: 'checkbox' },
];
