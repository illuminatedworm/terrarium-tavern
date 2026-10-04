# 开发验证与发行

所有命令从仓库根目录运行。先按[安装指南](../guides/maintenance.md)安装依赖；测试使用合成夹具、临时数据及本地模拟模型。

## 启动开发环境

```powershell
npm.cmd run dev
```

该命令同时启动后端和 Vite，访问 `http://127.0.0.1:5173`。如果 8000 端口已有本项目后端，脚本复用它；否则启动后端并在退出时关闭。也可只执行 `npm.cmd run dev:web`，另行运行 `start.ps1`。

8000 端口提供上次构建的页面，需要刷新静态资源时执行 `build.ps1` 或 `npm.cmd run build`。

## 按改动选择验证

| 改动范围 | 主要检查 |
|---|---|
| 设置和保存 | `test_settings_document.py`、`test_provider_model_settings.py`、`settingsDocument.test.ts`；浏览器 `browser_settings`、`browser_prompt_prefixes` |
| Half Agent／记忆 | `test_tracker.py`、`test_memory_windows.py`、`test_classified_memory.py`、`test_half_agent_alignment.py` |
| Full Agent | `test_full_agent.py`、`test_full_agent_tools.py`、`test_full_agent_alignment.py`；浏览器 `browser_full_agent` |
| 工具过程和事件 | `test_full_agent_activity.py`、`agentActivity.test.ts`、`turnStream.test.ts` |
| 检索和窗口 | `test_hybrid_retrieval.py`、`test_conversation_lore.py`、`test_tokens.py` |
| 角色编辑与脚本 | `test_card_authoring.py`、`test_card_editor.py`、`test_card_editor_alignment.py`、`test_runtime.py`；浏览器 `browser_card_editor`、`browser_card_scripts` |
| 缓存和模型协议 | `test_unified_cache.py`、`test_cache_follow.py`、`test_sent_cache_breakpoints.py`、`test_model_api_modes.py` |
| 历史界面 | `storyHistory.test.ts`；浏览器 `browser_history_navigation`、`browser_history_rendering` |
| 存储与发行 | `test_backup.py`、`test_session_fork.py`、`test_repository_hygiene.py`、`test_release_quality.py` |

Python 文件位于 `tests/backend/`，TypeScript 文件位于 `tests/frontend/`。例如：

```powershell
.venv/Scripts/python.exe -m pytest tests/backend/test_tokens.py -q
npm.cmd test -- tests/frontend/storyHistory.test.ts
.venv/Scripts/python.exe -m tests.browser.browser_settings
```

重点保护协议、存储、权限、事务及资源生命周期。代表性边界包括失败、取消、版本冲突、分支、重生成和回调顺序；已解决的专用复现应在通用覆盖充分后收拢，避免无目的地枚举全部模型别名组合。

## 完整验证

```powershell
npm.cmd test
npm.cmd run format:check
npm.cmd run build
.venv/Scripts/python.exe -m ruff check src tests tools
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m tests.browser.browser_release
.venv/Scripts/python.exe -m tests.browser.browser_full_agent
```

后端全套包含真实 Terrarium／Chromium 运行与长程检查，执行时间比普通单元测试长。浏览器发行入口使用 8015/8016 端口，运行前需空闲；其他专项使用各自独立数据和端口。

发行浏览器检查覆盖设置、角色编辑、故事、重生成、工作记录、上下文、分支与 MVU，并使用 1440×900、1280×720、390×844 和 720×450 视口。最后一种模拟 200% 等效 CSS 视口，不代替所有浏览器的原生缩放检查。

日常修改运行受影响范围的检查；涉及跨模块行为或发行时扩大验证。通过后仅在新增修改、失败或尚未解决的问题需要时重复运行。

## 缓存链路评估

真实缓存评估从已运行应用中新建故事并连续提交回合，覆盖工具续答、断点推进和适用的恢复路径。保持提供商、模型、角色卡与生成参数一致，同一中转的不同协议分别评估。

以下只读命令导出工作记录中的实际用量、最终 HTTP 前缀与工具哈希：

```powershell
.venv/Scripts/python.exe tools/audit_runtime_cache.py --data data --session <会话ID> --output .cache/cache-audit.json
```

它不调用模型或修改数据库，也不导出凭据、剧情和工具参数。工具按当前规则重新解析原始 usage，标记与旧计数的差异，缺失字段保持未知。核对时同时检查前缀、实际读取／写入和账单；哈希相同不能证明服务端路由、协议转换或计费规则相同。

[probe_prefix_cache.py 的使用说明](../../tools/probe_prefix_cache.md)用于隔离消息缓存协议的合成对照，不代替实际应用中的工具和状态链路。

## 检索与模型质量评估

```powershell
.venv/Scripts/python.exe tools/evaluate_retrieval.py
.venv/Scripts/python.exe tools/evaluate_retrieval.py --neural --model-dir data/models
```

评估经过最终上下文编译，统计多语言正例和无关查询中的误注入、召回与延迟。神经模式只使用已下载模型。[replay_context.py](../../tools/replay_context.py)对显式指定的备份只读回放，私人数据与派生结果留在忽略目录中。

比较实际角色卡的本地模型时，可使用 `tools/benchmark_local_retrieval.py`。
提前在忽略目录准备 JSON 题集：`{"queries":[{"id":"q1","kind":"named","text":"查询","relevant":["目标条目 ID"]}]}`。
`kind` 可用 `named`、`paraphrase` 或 `negative`，无关查询的 `relevant` 为空数组。
分别以 `--variant 0.6b-bf16` 和 `--variant 4b-int8` 运行，并传入 `--card-name`、`--queries`、`--output`；
`--data` 默认指向应用的 `data`，模型必须已存在于该目录的 `models` 子目录。
该工具只读实际应用数据库，在报告目录建立独立索引，使用相同题集、批量和向量维度执行真实召回与重排。
它报告主目标命中率、MRR、延迟和整卡显存采样，不包含正文生成与最终上下文编译。
为避免超时掩盖模型结果，测量时超时统一放宽到 120 秒；报告另外估算超过原有超时预算的请求数。

合成回归证明程序路径和契约，模型的叙事质量、记忆充分性、误判及延迟需要独立评估。保留报告时记录测试日期、代码版本、配置、样本与统计口径；无法确认的历史基线明确标为缺失。

## 打包与产物

成功构建后运行：

```powershell
.venv/Scripts/python.exe tools/package_release.py
```

ZIP、清单和校验值输出到 `releases/`。[verify_release.py](../../tools/verify_release.py)可检验归档并执行干净安装；该过程会下载依赖，代理沿用调用环境中的配置。

截图、原始日志、评估数据和私人回放只写入 `.cache/` 等忽略目录；可公开的脱敏调查归档到 `docs/reports/`。许可文件的版本、范围与未决发行事项见[许可与发行](licensing.md)。

## Card Editor 真实模型评测

`tools/evaluate_card_editor.py` 仅在明确授权后读取现有模型配置，在 `.cache/card-editor-evaluation/` 的隔离数据库中运行合成案例。`--cases core` 覆盖前三类核心场景，`--cases remaining` 覆盖其余场景。所有运行共用 `budget.json`，累计最多 60 次实际 HTTP 请求，达到上限停止并报告未验收范围。报告记录实际调用、用量、耗时和产物，创作语义仍按方案人工核对；不能用模拟工具调用或模型自评代替效果验收。
