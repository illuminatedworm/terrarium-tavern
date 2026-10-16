# 开发验证与发行

以下命令从仓库根目录运行。先按[安装指南](../guides/maintenance.md)准备依赖，测试使用独立数据和本地模拟服务。

## 启动开发环境

| 操作 | Windows（PowerShell） | Linux（Bash） |
|---|---|---|
| 同时启动后端与 Vite | `npm.cmd run dev` 或 `.\run-dev.ps1` | `./run-dev.sh` |
| 仅启动 Vite | `npm.cmd run dev:web` | `npm run dev:web` |
| 构建静态页面 | `.\build.ps1` 或 `npm.cmd run build` | `./build.sh` 或 `npm run build` |

开发页面为 [http://127.0.0.1:5173](http://127.0.0.1:5173)。开发脚本复用端口 8000 上已通过健康检查的本项目后端，或自行启动后端并在退出时关闭该进程。仅启动 Vite 时，需另行运行对应系统的 `start` 脚本。

端口 8000 提供最近一次构建的静态页面。构建更新 `dist/` 和 `src/tavern/static/web/`。设置、角色卡编辑器和制卡界面按需加载，调整加载边界后应分别打开这些页面，检查分块与样式。

`npm run dev` 的包脚本调用 Windows PowerShell，Linux 使用上表的 Bash 入口。

## 按改动选择验证

| 改动 | 主要检查 |
|---|---|
| 设置与保存 | `test_settings_document.py`、`test_provider_model_settings.py`、`settingsDocument.test.ts`；浏览器 `browser_settings` |
| Half Agent 与记忆 | `test_tracker.py`、`test_memory_windows.py`、`test_classified_memory.py`、`test_half_agent_alignment.py` |
| Full Agent | `test_full_agent.py`、`test_full_agent_tools.py`、`test_full_agent_alignment.py`；浏览器 `browser_full_agent` |
| 工具事件与流式显示 | `test_full_agent_activity.py`、`agentActivity.test.ts`、`turnStream.test.ts`；浏览器 `browser_full_agent_streaming` |
| 检索与上下文 | `test_hybrid_retrieval.py`、`test_conversation_lore.py`、`test_tokens.py` |
| 制卡、脚本与变量 | `test_card_authoring.py`、`test_card_editor.py`、`test_runtime.py`、`test_script_permissions.py`、`test_mvu_compatibility.py`；浏览器 `browser_card_editor`、`browser_mvu_panel`、`browser_mvu_formats` |
| 缓存和模型协议 | `test_unified_cache.py`、`test_cache_follow.py`、`test_sent_cache_breakpoints.py`、`test_model_api_modes.py`、`test_genai_transport.py`；Gemini 设置浏览器 `browser_genai` |
| 历史阅读 | `storyHistory.test.ts`；浏览器 `browser_history_navigation`、`browser_history_rendering` |
| 存储、备份和发行 | `test_backup.py`、`test_session_fork.py`、`test_repository_hygiene.py`、`test_release_quality.py` |
| Full Agent 工具文档 | `test_full_agent_documentation.py`；`tools/export_full_agent_tools.py --check` |

Python 测试位于 `tests/backend/`，TypeScript 测试位于 `tests/frontend/`。例如：

```powershell
.venv/Scripts/python.exe -m pytest tests/backend/test_tokens.py -q
npm.cmd test -- tests/frontend/storyHistory.test.ts
.venv/Scripts/python.exe -m tests.browser.browser_settings
```

Linux 使用 `.venv/bin/python` 和 `npm`，下文命令同理。

回归重点是协议、事务、权限、版本冲突、取消、重生成、分支和资源生命周期。优先补充已有场景，参数化覆盖确实影响行为的维度；纯工具检查使用内存数据，存储和完整回合检查使用独立数据库。测试成本同时看覆盖范围和实际耗时。

面板资源改动还应检查 `test_script_resources.py`、`storyResources.test.ts`、`storyRuntime.test.ts` 和 `browser_story_resources`。这些用例覆盖历史读取、回滚、分支、iframe 复用及离屏恢复。角色卡顶层字段改动应检查 `test_card_context_blocks.py` 和两种模式的 alignment 用例，核对最终请求中的顺序、标签、宏展开和完整原文。

## 完整验证

跨模块改动或发行前运行：

```powershell
npm.cmd test
npm.cmd run format:check
npm.cmd run build
.venv/Scripts/python.exe -m ruff check src tests tools
.venv/Scripts/python.exe -m pytest -q tests/backend -n 4 --dist worksteal
.venv/Scripts/python.exe -m tests.browser.browser_release
.venv/Scripts/python.exe -m tests.browser.browser_full_agent
```

全套后端测试通过 pytest-xdist 使用 4 个进程，每个用例保留独立数据。资源有限时可改为 `-n 2`，少量检查可串行运行；`--durations=20` 可定位耗时。后端测试包含真实 Terrarium／Chromium，耗时通常高于普通单元测试。

`browser_release` 使用 8015/8016 端口，覆盖设置、角色编辑、故事、工作记录、上下文、分支和 MVU；视口为 1440×900、1280×720、390×844 和 720×450，最后一项使用 DPR 2 模拟 200% 缩放。

布局专项还覆盖 2560×1440、1920×1080、360×792/DPR 3 和 960×540/DPR 2。DPR 场景验证对应 CSS 布局，原生浏览器缩放和真实手机交互可另行实测。

ChatGPT 订阅专项：

```powershell
.venv/Scripts/python.exe -m pytest -q tests/backend/test_chatgpt_auth.py tests/backend/test_chatgpt_provider.py tests/backend/test_chatgpt_integration.py
.venv/Scripts/python.exe -m tests.browser.browser_chatgpt
```

这些测试使用合成 OAuth 响应和隔离凭据，覆盖本地回调、模型传输及应用界面。真实账号的授权、资格和推理需在系统浏览器完成登录后单独检查。

## 缓存链路评估

真实缓存评估使用独立故事，连续提交回合，记录工具续答和断点推进。对照时保持提供商、模型、角色卡和参数一致，各接口协议分别统计。

```powershell
.venv/Scripts/python.exe tools/audit_runtime_cache.py --data data --session <会话ID> --output .cache/cache-audit.json
```

审计工具只读工作记录，输出实际用量、最终 HTTP 前缀和工具指纹。输出排除凭据、剧情和工具参数；缺失用量保持未知。核对时结合前缀变化、读取／写入量和账单。

[probe_prefix_cache.py](../../tools/probe_prefix_cache.md)提供合成消息的协议对照，真实应用另有工具和状态链路。

## 检索与模型质量评估

```powershell
.venv/Scripts/python.exe tools/evaluate_retrieval.py
.venv/Scripts/python.exe tools/evaluate_retrieval.py --neural --model-dir data/models
```

评估包含最终上下文编译，统计多语言正例、无关查询中的误注入、召回和延迟。神经模式使用已下载模型。[replay_context.py](../../tools/replay_context.py)可只读回放指定备份。

`tools/benchmark_local_retrieval.py` 可比较实际角色卡的本地模型。题集格式：

```json
{"queries":[{"id":"q1","kind":"named","text":"查询","relevant":["目标条目 ID"]}]}
```

`kind` 可选 `named`、`paraphrase`、`negative`；无关查询的 `relevant` 使用空数组。以 `--variant 0.6b-bf16` 和 `--variant 4b-int8` 分别运行，并传入 `--card-name`、`--queries`、`--output`。`--data` 默认为 `data`，模型从其 `models` 子目录读取。

该工具在报告目录建立独立索引，统计命中率、MRR、召回与重排延迟及显存采样。测量超时设为 120 秒，另统计超过原配置超时的请求数；正文生成和最终上下文编译由应用级评估覆盖。

程序回归检查契约，实际游玩评估叙事质量、记忆充分性和误判。结果保留日期、代码版本、配置、样本及统计口径。

## 打包与产物

构建完成后运行：

```powershell
.venv/Scripts/python.exe tools/package_release.py
```

ZIP、清单和校验值输出到 `releases/`。[verify_release.py](../../tools/verify_release.py)在 Windows 新目录校验归档、安装依赖、启动服务并检查备份恢复。该脚本带有本机工具与网络配置假设，运行前需核对其环境配置。

测试截图、日志、评估结果和私人回放放在受忽略的本地目录。公开文档图片放在 `docs/images/`。许可文件和发行未决事项见[许可维护](licensing.md)。

## 角色卡编辑 agent 真实模型评测

`tools/evaluate_card_editor.py` 使用现有模型配置并消耗模型额度，运行前需明确授权。默认在 `.cache/card-editor-evaluation/` 建立隔离数据；`--cases core` 和 `--cases remaining` 分别选择核心及其余场景。

同一输出目录的运行共用 `budget.json`，累计最多 60 次实际 HTTP 请求，达到上限后停止。记录包含调用、用量、耗时和产物；创作结果按已确认方案人工审阅。
