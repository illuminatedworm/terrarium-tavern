# 外部联网工具

参谋 agent 和角色卡编辑 agent 共用本地 `web_search`、`read_webpage`，分别受各自的联网开关控制。外部资料允许限量和分页，不适用角色卡及内部数据的一次全文读取要求。工具返回的片段、来源和限制信息一并保存并完整送往模型，传输阶段不再截短。

## 搜索

`web_search(query, limit=5, objective?)` 的 `limit` 是返回的来源记录数，合法范围 **1–10，含两端**。Exa、Tavily、SearXNG 和 DuckDuckGo 均在本地落实该限制，不依赖上游一定遵守请求参数。

- 每条标题最多 300 个 Unicode 字符，摘要最多 2,000 个字符；返回的 URL、标题和摘要合计最多 20,000 个字符，可能因此少于请求数量。
- URL 原样保留，不截短；超过 8,192 字符或格式无效时报错并按配置尝试其他服务。
- 返回 `results`、`returned_results`、`available_results`、`results_truncated`、`content_truncated`。`available_results` 仅表示本次服务响应中识别出的记录数，不是互联网全部命中数。
- 单条标题或摘要被限量时带 `title_truncated`／`snippet_truncated`。需要细节时读取该来源网页，不能将搜索摘要当作全文。
- Exa 文本先解析为来源记录，不再另附全部服务文本。无法识别来源记录的格式作为服务失败降级，不能用原始文本绕过限量，也不能冒充零命中。
- `web_sources` 只包含本次实际返回的来源，已省略的条目不进入引用列表。

## 网页读取

```typescript
{
  url: string;
  offset?: number; // 0-based Unicode 字符偏移，默认 0
  limit?: number;  // 默认 12,000，合法范围 1–30,000（含两端）
  service?: "exa" | "tavily" | "local";
}
```

仅接受无凭据的公共 HTTP(S) 网址，最长 8,192 字符；不接受内网或本地文件地址。`offset ≥ 0`，无固定上界；到达或越过本次可用文本末尾时返回空文本、`has_more=false`、`next_offset=null`。偏移是字符数，不是行号、字节或 token；分页可能跨越段落或表格。

首次可省略 `service`，使用启用服务的配置顺序，并在必要时降级到本地 HTML／文本提取。续读传上次返回的 `service` 和 `next_offset`，避免更换提取器导致位置不一致。指定服务不可用时直接报错，不自动切换；若需换服务，从 `offset=0` 重新读取。每次重新获取当前网页，不保存全文缓存，网页变化后也应从头读取。

返回 `url`、`title`、`text`、`offset`、`returned_chars`、`total_chars`、`has_more`、`next_offset`、`source_truncated`，以及实际 `service`、鉴权模式和尝试记录。标题最多 300 字符，必要时标明 `title_truncated`。

`total_chars` 只统计本次取得的提取文本；`has_more` 仅表示这份可用文本内还有内容。`source_truncated=true` 表示已知上游截断（包括本地收到 HTTP 部分响应），`false` 表示已完整接收本地提取输入或服务明确未截断，`null` 表示服务未说明。最后一页不代表原网页全文已读。可用但已截断的提取文本允许作为明确标记的片段返回，不为追求全文而强制切换服务。

HTTP 解码后的响应体仍限制为 8 MiB；Exa 的上游提取上限为 1,000,000 字符，达到时标记为来源已截断。两者都独立于每次送给模型的正文上限。服务调用保留配置超时，取消不触发降级。已知密钥在分页前脱敏，避免分页边界将密钥切开。

## 兼容与边界

旧的仅传 `url` 调用现在读取默认第一页，其他旧参数保持可用。已保存工具结果仍按原记录回放；新工具说明参与既有上下文策略检查，不覆盖用户自定义系统提示词，不需要数据库迁移。

官方自动模式中的服务端原生 `web_search` 不经过这些本地搜索适配器，其工具结果由服务端管理；本地 `read_webpage` 仍采用上述分页协议。工具目录、请求预览和实际调用使用同一参数模型。
