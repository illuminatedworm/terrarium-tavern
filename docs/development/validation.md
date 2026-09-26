# 验证与发行

从仓库根目录运行，先安装开发依赖。测试使用合成夹具、临时数据和本地模拟模型，不读取用户凭据。

```powershell
npm.cmd test
npm.cmd run format:check
npm.cmd run build
.venv/Scripts/python.exe -m ruff check src tests tools
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m tests.browser.browser_release
.venv/Scripts/python.exe -m tests.browser.browser_full_agent
```

- `tests/backend/`：API、存储、回合、记忆、检索、模型数据边界及脚本运行时回归。全套包含 200 轮 Terrarium/Chromium 长测，需要数分钟。
- `tests/frontend/`：前端状态、渲染、事件和 MVU 兼容测试，由 Vitest 收集。
- `tests/browser/`：使用独立数据的浏览器专项检查。使用 `python -m tests.browser.browser_名称` 运行；发行入口使用 8015/8016 端口，须预先空闲。
- `tests/support/` 和 `tests/fixtures/`：共享辅助函数及合成数据。夹具是测试输入，不是历史测量记录。

Full Agent 专项：`tests/backend/test_full_agent.py` 覆盖原生工具 HTTP 请求、换窗与回读、来源分页、检索仲裁、分支/重生成/编辑、取消回滚及真实 MVU/审核提交；浏览器专项覆盖模式选择、笔记、人工换窗和桌面/手机布局。

`test_full_agent_activity.py` 与前端 `agentActivity.test.ts` 验证对话工具记录的版本/分支归属、结果分页、SSE 去重与顺序、失败回合恢复和 debug 分页排序；浏览器专项同时验证默认折叠、参数/结果展开、刷新恢复和原始 JSON 查看。

浏览器发行检查覆盖设置、角色编辑、故事、重生成、记录、上下文、分支与 MVU，并检查 1440×900、1280×720、390×844 和 720×450 的视口。最后一种是 200% 等效 CSS 视口，不能代替所有浏览器的原生缩放验证。

## 可选评估和打包

```powershell
.venv/Scripts/python.exe tools/evaluate_retrieval.py
.venv/Scripts/python.exe tools/package_release.py
```

检索评估的神经模式需另加 `--neural --model-dir data/models`，只使用已下载模型。`tools/replay_context.py` 只对显式指定的备份进行只读回放。

发行打包前必须成功构建；ZIP、清单与校验值输出到 `releases/`。可用 `tools/verify_release.py` 检验归档并执行干净安装，该命令会安装依赖，境外资源使用指定代理。

截图、日志、评估结果只写入 `.cache/` 等被忽略的目录，不提交具体报告、私人回放或运行记录。测试通过仅证明对应程序路径，不代表任意社区卡和真实模型的语义质量。
