# Token、字符与字节

界面中的预计输入用于判断上下文占用，实际输入输出取自服务商返回。两者可能不同；比较费用时使用实际用量和账单，比较本地窗口压力时使用同一编译结果。

## 计数口径

| 项目 | 单位 | 计数方式 |
|---|---|---|
| 模型上下文窗口、输出预算 | tokens | 用户根据实际接入配置 |
| Half Agent 日常预算、历史预算、配方提示阈值 | tokens | Writer 模型选择的 BPE 分词器；单块计算完整正文 |
| Tracker 分批、输出与活跃记忆 | tokens | Tracker 模型选择的分词器 |
| Full Agent 窗口、checkpoint 和笔记 | tokens | Full Agent 模型选择的分词器；请求估算包含工具与原生续答内容 |
| 模型请求估算 | tokens | 消息、封装、工具定义及结果；预算行为见下文 |
| 上次实际输入、输出和缓存 | tokens | 按接口归一化展示，原始 usage 另行保留 |
| 检索查询与线索输入范围 | 字符 | Python Unicode 码点，包括空格与换行 |
| Embedding／Reranker 切片 | tokens | 对应本地模型的 tokenizer |
| BM25 切片 | 词元 | 中文、日文、英文分词及二元组结果 |
| 日志与存储大小 | 字节、KiB、MiB | 磁盘／UTF-8 大小 |

选中的世界书正文完整注入，不用检索输入字符限额截短它。神经检索切片用于查找，最终返回的是完整条目。

## 估算精度与预算行为

本地使用 tiktoken，随包提供 `o200k_base` 与 `cl100k_base` 词表，首次计数无需下载。各模型可自动匹配或指定分词器；无法匹配的名称使用 `o200k_base` 作为参考。

文本 BPE 计数是所选分词器的实际结果，但不一定等于目标服务商的分词。消息、工具封装和原生推理块也只能在本地估算，所以界面区分“预计输入”和“上次实际输入”。

Half Agent 可用输入容量扣除输出预算与 2048 tokens 余量，用于窗口压力计算。Full Agent 的强制换窗看当前上下文本身是否达到窗口上限，不预扣回复额度。通用请求诊断可以同时显示含输出和余量的超量提示；该提示与强制换窗触发不是同一条件。

已选文本不会因为本地估算超量而被截断或省略，应用提示后完整发送，由服务商决定是否接受。记忆条目数量、checkpoint／笔记长度和活跃记忆容量仍有各自的内容校验，详见[文本完整性](../design/text-integrity.md)。

## 查看占比

上下文预览与实际发送使用同一计数入口。API 的 `estimated_tokens`、`token_counting`、`budget_sources` 表示总估算、分词器信息与来源分组；trace 中的 `content_tokens` 是正文计数，`estimated_tokens` 还包含对应封装，单位为 `tokens`。

来源分组合计等于总估算，请求封装开销单独列出。预览不执行本地神经召回，因此实际发送时世界书命中可能改变。用量字段缺失保持未知，不能当成零消耗，见[协议用量口径](../reference/model-api.md#原生回复与用量)。

## 实现入口

计数位于 [core/tokens.py](../../src/tavern/core/tokens.py)，词表清单位于 [tokenizer_data/manifest.json](../../src/tavern/tokenizer_data/manifest.json)。维护词表使用 [prepare_tokenizers.py](../../tools/prepare_tokenizers.py)，该工具校验随包 SHA-256；更新资源后执行 [test_tokens.py](../../tests/backend/test_tokens.py)。
