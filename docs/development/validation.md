# 开发验证与发行

以下命令从仓库根目录运行。先按[安装指南](../guides/maintenance.md)准备依赖，测试使用独立数据和本地模拟服务。

## 启动开发环境

| 操作 | Windows（PowerShell） | Linux（Bash） |
|---|---|---|
| 同时启动后端与 Vite | `npm.cmd run dev` 或 `.\run-dev.ps1` | `./run-dev.sh` |
| 仅启动 Vite | `npm.cmd run dev:web` | `npm run dev:web` |
| 构建静态页面 | `.\build.ps1` 或 `npm.cmd run build` | `./build.sh` 或 `npm run build` |

开发页面为 [http://127.0.0.1:5173](http://127.0.0.1:5173)。开发脚本复用端口 8000 上已通过健康检查的本项目后端，或自行启动后端并在退出时关闭该进程。仅启动 Vite 时，需另行运行对应系统的 `start` 脚本。`.\run-dev.ps1 -Desktop` / `./run-dev.sh --desktop` 打开可选的受控窗口：入口独立拥有 Vite、后端和窗口，任一服务失败或窗口关闭时统一收尾，端口被占用则报错，不接管已有服务。

卡片 UI 的浏览器验收在导航前安装 `tavern.scripting.network_guard.NETWORK_GUARD`，模拟受控宿主。`browser_mvu_panel` 默认单视口，覆盖宿主隔离、嵌套文档 WebRTC 回环探针、普通桌面浏览器照常运行面板与缺少隔离的 Android 宿主暂停；布局变更时使用 `--all-viewports`。

端口 8000 提供最近一次构建的静态页面。构建更新 `dist/` 和 `src/tavern/static/web/`。Vue 运行时单独打包为共享分块，入口与按需页面共用。设置、角色卡编辑器和制卡界面按需加载，调整加载边界后应分别打开这些页面，检查分块与样式。

`npm run dev` 的包脚本调用 Windows PowerShell，Linux 使用上表的 Bash 入口。

## 按改动选择验证

| 改动 | 主要检查 |
|---|---|
| 设置与保存 | `test_settings_document.py`、`test_provider_model_settings.py`、`settingsDocument.test.ts`；浏览器 `browser_settings` |
| Half Agent 与记忆 | `test_tracker.py`、`test_memory_windows.py`、`test_classified_memory.py`、`test_half_agent_alignment.py` |
| Full Agent | `test_full_agent.py`、`test_full_agent_tools.py`、`test_full_agent_alignment.py`；浏览器 `browser_full_agent` |
| 工具事件与流式显示 | `test_full_agent_activity.py`、`agentActivity.test.ts`、`turnStream.test.ts`；浏览器 `browser_full_agent_streaming --single-viewport`（布局验收时省略参数） |
| 检索与上下文 | `test_hybrid_retrieval.py`、`test_conversation_lore.py`、`test_tokens.py` |
| 制卡、脚本与变量 | `test_card_authoring.py`、`test_card_author.py`、`test_runtime.py`、`test_script_permissions.py`、`test_mvu_compatibility.py`；浏览器 `browser_card_author`、`browser_mvu_panel`、`browser_mvu_formats` |
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

仅在发行前或用户明确要求时运行：

```powershell
npm.cmd test
npm.cmd run format:check
npm.cmd run build
.venv/Scripts/python.exe -m ruff check src tests tools
.venv/Scripts/python.exe -m pytest -q tests/backend
.venv/Scripts/python.exe -m tests.browser.browser_release
.venv/Scripts/python.exe -m tests.browser.browser_full_agent
```

日常改动只运行上表中直接相关的文件。全套后端可用 `-n 4 --dist worksteal` 并行。`tests/conftest.py` 在测试进程内关闭 SQLite 同步写盘，以减少磁盘压力，不影响应用本身。`--durations=20` 可定位耗时。

`browser_release` 使用 8015/8016 端口，覆盖设置、角色编辑、故事、工作记录、上下文、分支和 MVU；CSS 视口为 2560×1440、1920×1080、360×792/DPR 3（1080×2376 设备像素）和 960×540/DPR 2（1920×1080 的 200% 缩放等效布局）。

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

## 版本维护

项目版本采用 `0.MINOR.PATCH`：补丁版本用于现有功能修复与小幅完善，次版本用于新能力或核心行为调整；`0.x` 保持实验阶段，兼容变化须在[版本记录](../changelog.md)明确说明。进入 `1.0.0` 前应稳定核心行为和升级兼容约定，并完成发行验证。应用版本与数据库 schema、设置文档版本分别维护，不因应用升版而机械递增。

升版时同步 `pyproject.toml`、`package.json`、`uv.lock` 中本项目条目、`package-lock.json` 中根包的两处版本，以及 `src/tavern/application/api.py` 的 OpenAPI 与健康检查版本。锁文件中第三方依赖版本保持不变。同时更新 README、文档导航、安装维护说明和版本记录。

版本提交使用中文说明，创建带注释的 `vX.Y.Z` 标签。两种系统共用标签；`linux` 通过 rebase 同步 `main` 的通用修改，有平台独有提交时保留并检查适配。历史补标的范围与依据应明确记录，不能把补标日期或测试状态写成当年的正式发行记录。此后已公开的版本标签保持固定，修复使用新版本。

## 打包与产物

构建完成后运行：

```powershell
.venv/Scripts/python.exe tools/package_release.py
```

ZIP、清单和校验值输出到 `releases/`。[verify_release.py](../../tools/verify_release.py)在 Windows 新目录校验归档、安装依赖、启动服务并检查备份恢复。该脚本带有本机工具与网络配置假设，运行前需核对其环境配置。

Windows/Linux 便携包使用 `tools/package_portable.py` 在各自目标平台构建，附带独立 Python、官方 Node.js、
锁定依赖和 Chromium；`verify_release.py` 自动识别便携包，不再在线安装依赖。
详细命令、平台边界及隔离验证范围见[桌面开发与便携包](desktop.md)。

`verify_release.py` 和 `prepare_tokenizers.py` 沿用调用环境中的代理，不内置代理地址；`probe_prefix_cache.py` 默认直连，可用 `--proxy` 显式指定。应用及制卡评测使用应用内保存的网络配置。

测试截图、日志、评估结果和私人回放放在受忽略的本地目录。公开文档图片放在 `docs/images/`。许可文件和发行未决事项见[许可维护](licensing.md)。

## 制卡 agent 真实模型评测

`tools/evaluate_card_author.py` 使用现有模型配置并消耗模型额度，运行前需明确授权。默认在 `.cache/card-author-evaluation/` 建立隔离数据；`--cases core` 和 `--cases remaining` 分别选择核心及其余场景。

同一输出目录的运行共用 `budget.json`，累计最多 60 次实际 HTTP 请求，达到上限后停止。记录包含调用、用量、耗时和产物；创作结果按已确认方案人工审阅。
