# Terrarium Tavern

**让设定在需要时进入故事，让记忆随故事逐步积累。**

![开发阶段](https://img.shields.io/badge/%E9%98%B6%E6%AE%B5-%E5%AE%9E%E9%AA%8C%E6%80%A7-f59e0b)
![Windows](https://img.shields.io/badge/Windows-10%20%2F%2011-0078D4)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![Vue](https://img.shields.io/badge/Vue-3-4FC08D?logo=vuedotjs&logoColor=white)
![TypeScript](https://img.shields.io/badge/TypeScript-5.8-3178C6?logo=typescript&logoColor=white)

Terrarium Tavern 是基于 [Kohaku Terrarium](https://github.com/Kohaku-Lab/KohakuTerrarium) 的本地角色扮演应用。本项目类似 Silly Tavern 且兼容其部分生态，围绕长篇互动叙事组织角色卡、世界书、对话分支和剧情记忆，探索如何在有限上下文中维持设定与故事的连续性。

项目目前处于实验阶段，面向 Windows 本机使用。支持文本角色卡，以及带 HTML 界面和 MVU 变量的角色卡；模型通过兼容 OpenAI 的 API 接入，不同功能部分可以使用不同模型。

## 核心设计

### 两种独立运行模式

创建故事时可选择 **Half Agent**（原有模式）或 **Full Agent**（新模式）。旧会话默认 Half Agent，剧情分支继承原模式。

| 模式 | 写作与记忆 | 上下文管理 |
|---|---|---|
| Half Agent | Writer 写正文，Tracker 整理分类记忆、分析变量 | 运行时规划窗口与周期维护 |
| Full Agent | 一个 Agent 通过工具检索、维护笔记、更新变量并生成正文 | 当前窗口 + checkpoint + 可搜索原始历史 + 模型主动换窗 |

Full Agent 保留自动世界书检索与中途注入，也允许模型自拟 query 调用同一索引。记忆采用随窗口携带的交接笔记与按需读取的自由笔记，不运行后台分类记忆整理。详见 [Full Agent 架构、工具与消息边界](docs/design/full-agent.md)。

### 在对话中途注入世界书

不同于 ST 默认的顶部可变世界书条目注入，本应用的上下文设计中，只有常驻条目被注入到顶部且不会变化。

而基于历史消息召回的世界书条目，采用 **mid-conversation system prompt** 的注入方式：把检索到的相关设定作为系统消息，放在对话历史中与其对应的位置。

条目与触发它的剧情进度关联，在上下文中保持位置，并不因重复命中而变更位置或复制。随着对应消息退出上下文，条目一同退出并进入冷却。

这让世界书注入具有明确的时机与生命周期，借助“近因效应”的原理提升条目效果。

### 多路世界书索引与检索

世界书检索结合**关键词匹配、BM25 全文检索和可选的 embedding 检索**，并可使用 reranker 模型进一步筛选。关键词与 BM25 路线不需要 GPU，这也是默认方案。embedding 检索和 reranker 使用本地 Qwen3 模型。

各路候选经过融合、相关性准入和预算检查，递归触发也遵循相同约束。每组条目注入存在上限数量，宁缺毋滥。

三路并行召回并启用 reranker 时，即便只使用 Qwen3 0.6B 系列模型，在 RTX 12G 显卡上已能够达到低延迟快响应、高召回准确率之间的良好平衡。

### Half Agent：周期性触发的积累式记忆

后台按周期整理已提交剧情，默认每五轮触发一次；历史即将退出上下文时，还会检查相关剧情是否已经完成记忆整理。

记忆采用增量操作，逐步积累当前进展、变化与经历、未完成事项和待确认内容。已有条目不会仅因本轮没有被提到就消失，手工修订受到保护。模型负责判断事实与变化，程序负责回填原文来源、校验引用并提交状态。用户可以查看来源、编辑条目或手动重建记忆。

## 技术路线

前端使用 **Vue 3、TypeScript 和 Vite**；后端使用 **Python、FastAPI 和 SQLite**，以 Kohaku Terrarium 作为 agent 运行基座，借助隔离的 Chromium 环境运行角色卡脚本。

- **前台写作（Writer）**：负责叙事，普通正文生成不调用工具。
- **后台整理（Tracker）**：负责积累式记忆、按需变量分析，以及经用户授权的设定维护。
- **角色扮演运行时**：负责检索、上下文组装、分支、权限和事务提交。模型输出与脚本处理成功后，正文和状态才一起生效。

上述 Writer / Tracker / Roleplay Runtime 属于 Half Agent。Full Agent 使用独立的单模型工具循环和轻量执行宿主，共享检索、存储、模型传输和卡片脚本等底层能力。

## 安装

准备好 **Python、Node.js 22+ 和 Git**。项目运行环境固定为 Python 3.12，安装脚本通过 uv 管理依赖和所需的 Python 版本。

克隆本仓库，或下载并解压源码后，在项目根目录打开 PowerShell：

```powershell
.\setup.ps1
.\start.ps1
```

安装脚本会安装锁定依赖、构建前端并下载 Chromium。启动后访问 [http://127.0.0.1:8000](http://127.0.0.1:8000)。

安装依赖和下载资源时，视网络环境可能需要代理。安装脚本通过 `-Proxy` 参数指定代理服务的完整 HTTP(S) 地址（含协议与端口）：

```powershell
.\setup.ps1 -Proxy '<你的代理服务地址>'
```

不需要代理时，可显式使用 `setup.ps1 -Proxy ''`。

模型 API 的代理在应用设置中单独配置。若需本地向量检索和重排，使用 `setup.ps1 -LocalRetrieval` 安装额外依赖，再在应用设置中下载并预热模型；这条路线目前需要支持 CUDA 的 NVIDIA GPU。

停止服务：

```powershell
.\stop.ps1
```

自定义端口使用 `start.ps1 -Port 8001`，自定义数据目录使用 `start.ps1 -Data 'D:\MyStories'`。详细说明见[安装与维护](docs/guides/maintenance.md)。

## 开始使用

1. **配置模型**：在“设置 → 通用 → 提供商”添加连接，再分别配置 full agent 或 half agent 的模型。两种模式参数独立；half agent 内的正文生成和后台整理也分别选择模型。普通参数自动保存，提示词点击绿色勾保存；详见[设置说明](docs/guides/settings.md)。后台历史补查等功能需要模型支持工具调用。
2. **导入角色卡**：可以先试用 `samples/雾港来信.json`；`samples/星航状态卡.json` 用于体验 HTML/MVU，**使用前需启用该卡的脚本权限**。
3. **开始故事**：选择角色卡、运行模式、玩家身份和开场。Full Agent 需要支持原生工具调用的模型，可单独配置，否则沿用 Writer 的连接与预算。需要更新变量或修改设定时，按界面提示明确授权。
4. **查看与调整**：通过上下文预览查看注入内容，通过后台记录了解整理过程；可编辑记忆、重新生成回复，或从选定进度创建独立分支。

数据默认位于 `data/`，其中包含游玩剧情、角色资源和模型配置。

## 当前局限与未完部分

- **角色卡编辑 agent 仍比较粗糙。** 已提供草稿编辑、工作流适配、校验和保存流程，但交互打磨、复杂卡片理解及修改质量仍需完善。请人工审阅修改结果，尤其是初始化、脚本与世界书用途的调整。
- **角色卡兼容范围有限。** 项目不是完整的 SillyTavern 运行环境，不能保证任意扩展、远程脚本或复杂 MVU 卡片都能直接使用，详见[兼容范围](docs/guides/compatibility.md)。
- **长篇叙事质量仍在探索。** 通过开发者个人游玩体验，初步确认中篇体验较好。具体模型、系统提示词、各种机制的参数数字都显著影响体验质量。
- **记忆系统对模型智能有要求** 由于记忆系统的复杂性和格式化输出要求，经测试，GPT-5.6-Luna 故障、错误率极高，至少需要配置 GPT-5.6-Terra 水平的模型才能正常工作。
- **无法破限或者效果有限** 本项目不支持 ST 社区中常见的重型预设。

  > 当前 ST 社区所用的各种破限重型预设，往往基于情景诱导、role 伪装欺骗等提示词编织操作，还需要辅以前端显示处理。
  > 开发者个人认为，此类沉重的提示词工程有妨碍输出质量之可能，且容易降低 AI API 缓存命中率。
  > 同时基于开发者个人趣味，破限功能长期不作考虑和实现。

- **Half Agent 的 Agent 化程度有限** 该模式中只有后台 tracker 和角色卡编辑 agent 才算严格意义上的 AI agent。前台 writer 实质是持有零工具的聊天助手。Full Agent 是独立实验路线，实际叙事质量需要另行验证。

  > 这是有意的，开发者个人观察后认为：当前各家模型后训练过于注重 agentic 和 coding，writer 若进入 agent 思维链路会“变得机械”，不利于角色扮演/创意写作。

## 文档与开发

- [文档导航](docs/README.md)
- [架构与职责](docs/design/architecture.md) · [上下文生命周期](docs/design/context-lifecycle.md)
- [世界书检索](docs/guides/retrieval.md) · [积累式记忆](docs/design/memory-consistency.md)
- [角色卡编辑](docs/guides/card-editor.md) · [项目目录](docs/development/structure.md)
- [验证与发行](docs/development/validation.md) · [开发约定](.agents/AGENTS.md)

常用开发命令：

```powershell
npm.cmd run dev            # 同时启动后端与 Vite，访问 http://127.0.0.1:5173，前端修改即时更新
npm.cmd run dev:web        # 仅启动 Vite；需另行运行 .\start.ps1
npm.cmd run build
.\build.ps1               # 更新 dist/ 和后端使用的静态页面
npm.cmd test
.venv/Scripts/python.exe -m pytest -q
```

也可直接运行 `.\run-dev.ps1`。若 8000 端口已有本项目后端，开发脚本会复用它；否则会启动后端并在退出时关闭。访问 8000 端口仍使用上次构建的页面；需要更新该页面时运行 `.\build.ps1`（等同于 `npm.cmd run build`）。

## 致谢

- [Kohaku Terrarium](https://github.com/Kohaku-Lab/KohakuTerrarium)：提供 agent 运行基座，是本项目的重要基础。
- [SillyTavern](https://github.com/SillyTavern/SillyTavern)：角色卡、世界书和互动叙事工作流的重要灵感来源。
- [MagVarUpdate](https://github.com/MagicalAstrogy/MagVarUpdate)：提供 MVU 变量更新核心。
- [Tavern Helper](https://github.com/n0vi028/JS-Slash-Runner)：为角色卡脚本接口适配提供参考。
- 角色卡规范、Vue、FastAPI、tiktoken 等项目及其贡献者。

Powered by **KohakuTerrarium**。Copyright 2024–2026 Shih-Ying Yeh (KohakuBlueLeaf) and contributors。

第三方组件的来源、固定版本与许可说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
