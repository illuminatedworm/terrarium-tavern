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

日常修改只运行受影响模块的核心行为测试；全套与长程浏览器检查用于涉及相应范围的改动或发行验收。长期保留协议、存储、权限、事务和资源生命周期等行为约定，以代表性的边界覆盖为主，不枚举全部模型别名与思考档位的组合。针对单次供应商异常、已解决缺陷或临时调查新增的专用复现，在问题解决并有通用覆盖后移除。一次适当验证通过后，只有新增修改、失败或尚未解决的问题才扩大或重复检查。

Full Agent 专项：`tests/backend/test_full_agent.py` 覆盖原生工具 HTTP 请求、换窗与回读、来源分页、检索仲裁、分支/重生成/编辑、取消回滚及真实 MVU/审核提交；浏览器专项覆盖模式选择、笔记、人工换窗和桌面/手机布局。

`test_full_agent_activity.py` 与前端 `agentActivity.test.ts` 验证对话工具记录的版本/分支归属、结果分页、SSE 去重与顺序、失败回合恢复和 debug 分页排序；浏览器专项同时验证默认折叠、参数/结果展开、刷新恢复和原始 JSON 查看。

缓存实测应从已运行应用中新建故事并连续提交回合，覆盖工具续答与断点推进；合成 provider 脚本不能代替此流程。可用以下只读工具导出实际工作记录中的缓存用量、最终 HTTP 缓存前缀与工具哈希：

```powershell
.venv/Scripts/python.exe tools/audit_runtime_cache.py --data data --session <会话ID> --output .cache/cache-audit.json
```

该命令不调用模型，不修改数据库，不导出凭据、剧情或工具参数。它按当前解析规则重新读取原始 usage，并标记与旧记录不同的计数；缺失字段仍为未知。对照时保持提供商、模型、角色卡与生成参数相同，分别测试接口协议，并以提供商账单复核。哈希相同只证明本地已发送前缀相同，不证明中转内部转换、路由、缓存 TTL 或计费规则相同。

浏览器发行检查覆盖设置、角色编辑、故事、重生成、记录、上下文、分支与 MVU，并检查 1440×900、1280×720、390×844 和 720×450 的视口。最后一种是 200% 等效 CSS 视口，不能代替所有浏览器的原生缩放验证。

## 可选评估和打包

```powershell
.venv/Scripts/python.exe tools/evaluate_retrieval.py
.venv/Scripts/python.exe tools/package_release.py
```

检索评估的神经模式需另加 `--neural --model-dir data/models`，只使用已下载模型。`tools/replay_context.py` 只对显式指定的备份进行只读回放。

发行打包前必须成功构建；ZIP、清单与校验值输出到 `releases/`。可用 `tools/verify_release.py` 检验归档并执行干净安装，该命令会安装依赖，境外资源使用指定代理。

截图、原始日志、评估数据和私人回放只写入 `.cache/` 等被忽略的目录。经明确要求归档的脱敏综合报告可放入 `docs/reports/`，不包含凭据、私人剧情或完整运行记录。测试通过仅证明对应程序路径，不代表任意社区卡和真实模型的语义质量。

设置拆分专项：`tests/backend/test_settings_document.py` 覆盖实际生效值迁移、密钥保护、模式隔离、快照和页级撤回；`npm test` 包含保存响应合并回归；`.venv/Scripts/python.exe -m tests.browser.browser_settings` 覆盖自动保存、绿色勾保存、未保存离开提示、搜索、撤回及桌面／移动／缩放布局。测试数据和截图写入忽略目录 `.cache/settings-redesign`。
