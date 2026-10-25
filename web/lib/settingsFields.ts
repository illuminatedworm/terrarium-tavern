import type { SettingField } from '../components/settings/SettingsFields.vue';
import { sendShortcutOptions } from './composerShortcut';
export const shortcutFields: SettingField[] = [
  {
    path: 'send_shortcut',
    label: '输入时发送和换行',
    options: sendShortcutOptions,
    help: '用于剧情、参谋 agent 和制卡 agent 的输入框，保存后立即生效；Ctrl+Enter 方案在 macOS 上也接受 Command+Enter。放大编辑和历史编辑保留各自的快捷键。',
  },
];
export const readingFields: SettingField[] = [
  {
    path: 'reply_locate_start',
    label: '回复完成后定位到正文开头',
    type: 'checkbox',
    help: '跟随最新内容时，回复完成后跳到本条正文开头；关闭后停留在末尾。未跟随时始终保持当前阅读位置。保存后立即生效。',
  },
];
export const debugFields: SettingField[] = [
  {
    path: 'debug_full_agent_prefix',
    label: 'Full Agent 前缀消息稳定性测试',
    type: 'checkbox',
    help: '在内存中暂存最近两次 SDK 实际序列化的请求（含工具续答和重试），逐项比较 messages、tools 和其余参数，并在通知中报告公共前缀与首个差异。切换会话或模型不重置比较；关闭后在下一次 Full Agent 操作或退出应用时清除。',
  },
  {
    path: 'debug_failed_requests',
    label: '保留失败请求原始数据',
    type: 'checkbox',
    help: '保存失败的模型生成请求与已收到的原始响应，认证凭据脱敏。请求、响应各最多 8 MiB，超限或未收完整会标记。记录可能包含角色卡和剧情；关闭后已有记录仍按日志保留设置清理。',
  },
];
export const keminiFields: SettingField[] = [
  {
    path: 'kemini.enabled',
    label: '启用 Kemini 式实验功能',
    type: 'checkbox',
    help: '允许新建 Kemini 实验对话并继续已有的此类对话：正文参数传输、创作提示改写与小说填充仅作用于这些对话。',
  },
  {
    path: 'kemini.normal_disclaimer_enabled',
    label: '普通免责声明',
    type: 'checkbox',
    disabled: (value) => !value.kemini.enabled,
    help: '仅实验主开关开启时可调整，作用于全部 Kemini 实验对话；关闭主开关会保留此项选择。启用时要求模型追加声明，并在回复下单独折叠展示。',
  },
];
export const keminiTextFields: SettingField[] = [
  {
    path: 'kemini.filler_text',
    label: '小说填充文本',
    help: '绝对头部第 1 条。用户自行填写；保存后随主开关完整注入，空白时省略。',
  },
  {
    path: 'kemini.clear_prompt',
    label: 'CLEAR 指令',
    help: '绝对头部第 2 条。保存原文直接发送，不会清空应用里的对话记录或更改模型思考档位。',
  },
  {
    path: 'kemini.identity_prompt',
    label: '创作身份与文风',
    help: '绝对头部第 3 条。包含完整身份、写作规则与文风文本；不另从其他设置补入或覆盖。',
  },
  {
    path: 'kemini.info_prompt',
    label: '资料起始提示',
    help: '绝对头部第 4 条，默认包含参考资料说明和 Markdown 分隔符。之后才开始应用规则、角色资料等消息。',
  },
  {
    path: 'kemini.roleplay_prompt',
    label: '角色扮演追加要求',
    help: '历史之后、助手确认之前，提示遵守后续创作要求。每次请求只加入当前一份，不回放旧副本。',
  },
  {
    path: 'kemini.acknowledgement',
    label: '助手确认',
    help: '作为预置的助手消息加入上下文，不表示模型实际做出过这条回复。',
  },
  {
    path: 'kemini.thinking_prompt',
    label: '显式自检提示',
    help: '完整的交错思考格式与要求。默认用同一消息中的成对 <Interleaving> 标签包裹三段思考与正文；<thinking> 用于分离辅助记录。只控制显式输出，不控制模型内部推理。',
  },
  {
    path: 'kemini.content_prompt',
    label: '正文约束',
    help: '位于显式自检之后，设置正文语言、叙述人称、字数与收尾要求；默认以“用户所扮演角色”指代当前用户角色。',
  },
  {
    path: 'kemini.disclaimer_text',
    label: '普通免责声明文本',
    help: '保存完整提示，包括 <disclaimer_format> 内的说明、声明示例和格式要求。仅实验主开关和普通免责声明同时开启时使用，不再额外添加包装提示。',
  },
  {
    path: 'kemini.transport_prompt',
    label: '正文传输指令',
    help: '位于自检、正文约束与声明之后，末尾输出提示之前，保存文本直接发送。正文传输函数名为 kemini_emit_story，正文参数名为 content。',
  },
  {
    path: 'kemini.output_prompt',
    label: '末尾输出提示',
    help: '最后发送，包含首次显式思考与直接开始输出的完整要求。引用前面的 Interleaving 成对示例，不另放孤立开标签；不改变模型实际思考档位。',
  },
  {
    path: 'kemini.tool_description',
    label: '正文传输工具说明',
    help: '作为 kemini_emit_story 函数定义的 description 发送，可在上下文预览的工具定义中核对。',
  },
  {
    path: 'kemini.content_description',
    label: '正文参数说明',
    help: '作为 content 字符串参数的 description 发送；不改变参数类型与必填约定。',
  },
];
export const miscFields: SettingField[] = [
  ...shortcutFields,
  ...debugFields,
  ...keminiFields,
  ...keminiTextFields,
];
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
  {
    path: 'lore_injection_title',
    label: '注入时携带条目标题',
    type: 'checkbox',
    help: '开启后，正文请求中每条世界书正文前加一行【标题】。标题取条目名称（comment），为空时取 name；两者皆空则不加。包括常驻条目，切换后请求前缀会变化一次。',
  },
  number('lore_recursion_depth', '递归深度', 0, 16),
];
export const retrievalFields: SettingField[] = [
  {
    path: 'retrieval.title_in_retrieval',
    label: '条目标题参与检索',
    type: 'checkbox',
    help: '作用于非常驻的常规条目：标题计入关键词排序与向量切片、重排文本和条目主体判断。关闭后只用关键词与正文；向量索引会按新口径重建。',
  },
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
  {
    ...number('retrieval.index_timeout', '单条索引超时（秒）', 1, 3600),
    help: '按单条世界书或归档经历计时，默认 60 秒。长条目可增大；失败时不保存该条的部分向量。',
  },
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
  number('tools.max_steps', '每次发送或继续的模型调用上限', 1, 100),
  number('tools.max_duration_seconds', '每次发送或继续的总时限（秒，默认 900）', 30, 7200),
  {
    ...number('tools.pressure_ratio', '上下文占用提醒阈值（比例）', 0.25, 0.9, 0.05),
    help: '上下文估算 token 数达到窗口容量的此比例时，提醒模型准备换窗。0.7 表示 70%；提醒本身不强制换窗。',
  },
  number('tools.handoff_tokens', '当前交接上限（tokens）', 128, 16384),
  number('tools.chapter_tokens', '交接单章上限（tokens）', 64, 4096),
  number('tools.chapters_total_tokens', '交接章节总量上限（tokens）', 256, 65536),
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
  number('memory_recall_count', '归档经历召回上限（条，0 关闭）', 0, 20),
  {
    path: 'memory_recall_filter',
    label: '召回后由 Tracker 筛选',
    type: 'checkbox',
    help: '召回非空时增加一次后台请求；正文与每次脚本嵌套生成分别筛选，失败时沿用阈值准入结果。',
  },
];
export const requestFields: SettingField[] = [
  {
    path: 'model_request_mode',
    label: '模型请求方式',
    options: [
      { value: 'auto', label: '由各任务决定' },
      { value: 'stream', label: '流式' },
      { value: 'non_stream', label: '非流式' },
    ],
    help: '作用于普通 API 的正文、后台和制卡请求，从下一次操作生效；ChatGPT 订阅通道始终流式。Full Agent 的流式正文先显示为未保存预览，完整校验通过后才保存。',
  },
];
export const proxyFields: SettingField[] = [
  {
    path: 'network.proxy',
    label: '代理地址',
    type: 'text',
    placeholder: '留空直连',
    help: '完整的 HTTP(S) 代理地址，再在下方选择使用代理的请求。不读取系统环境中的代理设置。',
  },
  {
    path: 'network.api_proxy',
    label: '模型接口使用代理',
    type: 'checkbox',
    help: '也用于联网搜索和网页读取。模型请求还需对应提供商开启“使用接口代理”。',
  },
  { path: 'network.hf_proxy', label: '本地模型下载使用代理', type: 'checkbox' },
  { path: 'network.resources_proxy', label: '角色卡外部资源使用代理', type: 'checkbox' },
];
export const networkFields: SettingField[] = [...requestFields, ...proxyFields];
