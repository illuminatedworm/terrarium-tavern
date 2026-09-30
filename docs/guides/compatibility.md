# 兼容矩阵（0.1）

“可导入”只表示数据可解析。运行支持以表中具体行为与测试为准；尚未对大量社区角色卡做覆盖率统计。

| 能力 | 实现与边界 |
|---|---|
| V1/V2/V3 JSON | 保留原对象与未知字段；编辑后原格式导出 |
| PNG | chara / ccv3 tEXt，优先 V3；CRC 检查，保留其他 PNG chunks |
| CHARX | card.json 与内嵌资源，路径/大小限制，资源保真导出 |
| 世界书 | 内嵌、独立 JSON；保守关键词准入、辅助逻辑、常驻、概率、受原始查询约束的递归、深度、滚动注入及离开请求后概率冷却（旧持续字段保留但不执行） |
| 正则 | 原生 JavaScript RegExp；显示/模型阶段、placement 与 min/max depth |
| 原始提示词 | 角色正文、system、post history、开场与示例映射到配方 |
| 宏 | char、user、getvar / getglobalvar 常用读取；未知宏保留 |
| HTML 美化 | 独立 sandbox iframe；HTML/CSS/JS，尺寸通知；图片等依赖须缓存/内嵌；CSS 的 url()/@import 递归缓存，字体内嵌后离线显示图标，旧对话在打开时补齐 |
| 角色脚本 | 固定 SDK、无宿主密钥；按回合重新建立，变量持久化 |
| 消息变量回调 | 在服务端重建消息 DOM realm，顺序执行 MVU 回调并等待完成 |
| MVU | 使用固定提交的上游更新和 Schema 核心；InitVar、stat_data、更新事件、旧事件名别名 |
| MVU 命令 | 上游 set/add/insert/delete/move 与 JSON Patch 解析；上游错误通知提升为回合失败 |
| 变量 API | chat、message、character、script、global、preset、extension 镜像；所有 scope 隔离在会话分支中 |
| 消息读取 | 楼层号、负索引、latest、简单范围；返回角色、正文、变量和 ID |
| 消息写入 | 回合脚本镜像；界面写入仅支持当前楼层，历史操作需先切换分支 |
| 世界书写入 | 受设定修改策略控制，保存为当前分支的内容草稿 |
| generate / generateRaw | 当前模型、文本、取消、流式通知、覆盖和有序内容块；不加载 ST 预设 |
| 外部模块 | 下载和哈希缓存，离线 esbuild 打包静态可解析的浏览器模块 |
| 中途 system | 所有端点原位发送；能力检查仅诊断，不支持时显示实际错误 |

## 明确不等同于 ST 的部分

- 不提供完整 ST DOM、内部模块、完整 `SillyTavern.getContext()` 或通用 STscript。
- `global`、`character`、`preset` 等兼容变量目前是**会话分支内的命名空间**，不会跨角色或跨会话共享，避免分支外副作用。
- 角色脚本跨回合重新加载；需持久化的内容必须写入变量，不能依赖驻留闭包或定时器。
- 世界书支持角色前后、作者注释、示例前后、深度与命名 outlet；复杂 JavaScript 世界书正则不等价于 Python 正则，超限或不支持时报错。
- 未实现任意 EJS 模板插件、额外模型预设、ST 的代理管理或插件安装逻辑。
- generateRaw 支持单独的角色描述、性格、场景、世界书、示例、历史与用户输入占位符；复杂 ST 预设脚本不在其支持范围内。
- 消息 HTML 中的原生 ES module script 尚不能直接内联；应将模块放在角色脚本中，或由卡作者适配。
- 下载清单不会绕过访问控制或自动完成网站登录。任意动态 URL、CDN 特殊加载器和 ST 相对导入可能需要适配。
- 不宣称支持所有 MVU/Zod 第三方扩展。当前直接复用的上游核心行为与本应用宿主适配需分别验证。

## 模型思考档位

界面中的思考强度是共用选项，实际字段按接口协议发送；默认表示省略思考控制参数，让服务商采用模型默认行为。

| 接口 | 发送方式 |
|---|---|
| Chat Completions | 文字 `reasoning_effort` 或 `reasoning.effort`；在模型配置的“更多参数”选择格式，中转的实际支持以其协议为准 |
| Responses | 文字 `reasoning.effort` |
| Anthropic Messages | 支持 adaptive 的 Claude 使用 `thinking.type=adaptive` 和文字 `output_config.effort`；不生成 `budget_tokens` |
| Gemini 原生 | Gemini 3 使用文字 `generationConfig.thinkingConfig.thinkingLevel`；Gemini 2.5 使用其原生 `thinkingBudget` 协议 |

Claude Opus 4.6/4.7/4.8/5/5.5、Sonnet 4.6/5/5.5 及已登记的 Fable/Mythos 模型使用 adaptive。Opus 4.5 可以单独发送 effort，但不因此启用旧式 extended thinking。Sonnet/Haiku 4.5 等已知旧模型不支持文字档位，选择非默认档位时在请求前提示；未知中转别名直接发送标准 effort，由服务商校验。应用和 Claude SDK 都不会替旧模型把文字档位换算成思考预算。

共用选项不是各模型能力的承诺：Claude 的 minimal 映射到 low；不支持 xhigh、但支持 max 的 adaptive 模型映射到 max，Opus 4.5 的 xhigh 映射到 high。Gemini 3 的 xhigh 映射到 HIGH，Pro 的 minimal 映射到 LOW。Opus/Sonnet 5.5、Gemini 3 和 Gemini 2.5 Pro 不支持完全关闭思考，选择 none 会在请求前提示。新 Claude 不支持的采样控制不会发送；不支持强制工具选择的模型使用 auto 并只暴露目标工具，Full Agent 仍由运行时校验换窗要求。

协议参考：[Claude thinking](https://platform.claude.com/docs/en/build-with-claude/thinking)、[Claude effort](https://platform.claude.com/docs/en/build-with-claude/effort)、[OpenAI reasoning](https://developers.openai.com/api/docs/guides/reasoning)、[Gemini ThinkingConfig](https://ai.google.dev/api/generate-content#ThinkingConfig)、[Gemini 的 OpenAI 兼容映射](https://ai.google.dev/gemini-api/docs/openai)。

## 测试样本

- `samples/雾港来信.json`：原创文本卡、中文世界书、角色提示词与未知扩展。
- `samples/星航状态卡.json`：原创美化卡、按钮生成、InitVar、能量变量与修正回调。
- 测试动态构造 PNG 和 CHARX，检查导入/导出；浏览器测试运行真实 Chromium 与 KohakuTerrarium。
- 上游差分测试比较固定输入下的 MVU 核心与适配流程输出。

这些样本验证了具体闭环，不代表社区卡总体兼容率。遇到未支持接口时应提供兼容报告和可复现卡片片段。

## 已验证的 MVU-Zod 补强

支持 StageDog registerMvuSchema 依赖链、getAllVariables、klona、errorCatched，以及带 transform 的界面 Schema 导出。原卡导入 MVU artifact 时使用宿主已有的固定核心，避免启动第二份 ST 框架。远程辅助模块仍按卡缓存和记录哈希；支持范围由隔离夹具回归检查，不代表所有第三方扩展均兼容。

## 本版行为说明

关键词不再代表无条件强触发，单字/泛词以及重排拒绝的条目不会经递归复活。
中途条目与剧情共同计入窗口。手动编辑原文只更新文本和失效相关记忆，不自动重演后续变量；需从较早进度重生成或显式修改变量。
