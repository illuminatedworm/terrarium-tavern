# 外部联网工具

参谋 agent 和角色卡编辑 agent 共用本地 `web_search` 与 `read_webpage`，各自的联网设置决定可用方式。工具返回的来源、片段和限制信息完整保存并传入模型。

## 搜索

`web_search(query, limit=5, objective?)` 返回公共网页的来源摘要。

| 参数 | 范围与含义 |
|---|---|
| `query` | 必填，1–1000 字符 |
| `limit` | 默认 5，1–10 条来源记录，包含两端 |
| `objective` | 可选，1–4096 字符；省略时以 query 为检索目标 |

Exa、Tavily、SearXNG 和 DuckDuckGo 的结果统一在本地限量：标题最多 300 字符，摘要最多 2,000 字符，返回的 URL、标题和摘要合计最多 20,000 字符。URL 保留原样，长度上限 8,192 字符；无效格式或超长 URL 作为服务错误处理。

响应包含 `results`、`returned_results`、`available_results`、`results_truncated`、`content_truncated`。`available_results` 统计该次服务响应中识别出的来源数；单条省略用 `title_truncated` 和 `snippet_truncated` 标记。`web_sources` 记录实际返回的来源。

Exa 文本先解析为来源记录，再统一限量；无法识别的格式触发服务失败处理，合法零命中返回空列表。需要细节时，用网页读取工具取得对应来源片段。

## 网页读取

```typescript
{
  url: string;
  offset?: number; // Unicode 字符偏移，从 0 开始，默认 0
  limit?: number;  // 默认 12,000，范围 1–30,000，包含两端
  service?: "exa" | "tavily" | "local";
}
```

`url` 为无凭据的公共 HTTP(S) 地址，上限 8,192 字符。内网和本地文件地址会被拒绝。`offset` 最小为 0，无固定上界；到达或越过可用文本末尾时返回空文本、`has_more=false`、`next_offset=null`。

首次省略 `service` 时按启用服务的配置顺序尝试，必要时使用本地 HTML／文本提取。续读传上次返回的 `service` 与 `next_offset`，保持同一提取器。指定服务失败时返回错误；改用其他服务或网页已变化时，从 `offset=0` 重新读取。每次请求重新获取当前网页。

响应包含 `url`、`title`、`text`、`offset`、`returned_chars`、`total_chars`、`has_more`、`next_offset`、`source_truncated`，以及实际服务、鉴权方式和尝试记录。标题上限 300 字符，省略时标记 `title_truncated`。

`total_chars` 是本次可用提取文本的长度，`has_more` 表示这份文本还有后续片段。源完整性单独通过 `source_truncated` 表示：

| 值 | 含义 |
|---|---|
| `true` | 已知上游截断，包括 HTTP 部分响应 |
| `false` | 已完整接收本地提取输入，或服务明确说明返回完整 |
| `null` | 服务提供的完整性信息未知 |

分页结束与源完整性分别判断；已知截断的可用文本可以作为标明限制的片段返回。分页按 Unicode 字符计算，边界可能位于段落或表格中间。

HTTP 解码后的响应体上限为 8 MiB，Exa 上游提取上限为 1,000,000 字符。下载、上游提取和单页输出各有独立限额。服务调用使用配置超时，取消时结束请求；已知密钥在分页前脱敏。

## 兼容与边界

仅传 `url` 时读取默认第一页。已保存工具结果按原记录回放，新的参数说明随工具目录进入请求。

官方自动模式的原生 `web_search` 由服务端执行和管理结果；本地 `read_webpage` 使用上述分页协议。工具目录、预览与执行共用参数模型，定义见 [web/tools.py](../../src/tavern/web/tools.py)和[返回限制](../../src/tavern/web/results.py)。
