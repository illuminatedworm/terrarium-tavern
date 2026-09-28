# Token、字符与字节

## 当前口径

| 项目 | 单位 | 计数方式 |
|---|---|---|
| 模型上下文窗口、输出上限 | tokens | 用户根据服务商配置容量 |
| 日常上下文预算、历史预算、配方超长提示阈值 | tokens | 前台 Writer 选定的 BPE 分词器；单块只计算正文内容，不据此截断 |
| 后台输入分批、记忆输出预算 | tokens | 后台 Tracker 选定的 BPE 分词器 |
| 最终请求预算 | tokens | 计算消息、工具定义、工具结果，预留输出及 2,048 tokens 余量 |
| 对话区当前预计输入、上下文预览分组 | tokens | 同一编译结果，包含估算消息封装 |
| 上次实际输入/输出、缓存命中 | tokens | 服务商 usage 原值，不转换为字符或字节 |
| 世界书单次新增内容、检索输入和线索上限 | 字符 | Python Unicode 码点，包括空格、换行；不是 tokens |
| 神经检索切片与重叠 | tokens | 本地 Embedding/Reranker 自己的 tokenizer，独立于正文 |
| BM25 切片与重叠 | 词元 | jieba/Sudachi/英文分词结果，不是正文模型 tokens |
| 工作记录和日志存储大小 | 字节、KiB、MiB | UTF-8/磁盘大小，不参与上下文 token 预算 |

## 分词器与准确性

使用 tiktoken BPE 计数，内置 o200k_base 与 cl100k_base 词表，首次使用也无需下载。
Writer 和 Tracker 模型分别选择自动匹配或指定分词器。自动模式使用库提供的模型映射。
无法匹配的模型（例如当前 Gemini / DeepSeek 配置）使用 o200k_base 作为参考分词器，明确标为估算。
文本的 BPE tokens 是实际分词结果，但不能保证等于另一个服务商的专用分词结果；消息协议与工具封装也只能在本地估算。
因此发送前显示“预计输入 tokens”，返回后单独显示“上次实际输入 tokens”，实际用量以服务商报告为准。

预算检查、窗口压力和预览采用同一 token 口径，不再用 UTF-8 字节数或字符数冒充 tokens。
以前配置的数值保留，现在统一按 tokens 执行；如要维持过去以字节限制的紧凑程度，可调整日常 token 预算。

## 接口与旧记录

上下文 API 使用 estimated_tokens、token_counting、budget_sources；trace 每项含 estimated_tokens、content_tokens 和 unit=tokens。
预算来源合计等于总数，单独列出请求封装开销。
词表来源为官方 tiktoken；tools/prepare_tokenizers.py 通过指定代理获取并校验词表，SHA-256 随包保存。
