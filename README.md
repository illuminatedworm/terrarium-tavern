# Terrarium Tavern

**让设定在需要时进入故事，让记忆随故事逐步积累。**

![开发阶段](https://img.shields.io/badge/%E9%98%B6%E6%AE%B5-%E5%AE%9E%E9%AA%8C%E6%80%A7-f59e0b)
![Windows](https://img.shields.io/badge/Windows-10%20%2F%2011-0078D4)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![Vue](https://img.shields.io/badge/Vue-3-4FC08D?logo=vuedotjs&logoColor=white)
![TypeScript](https://img.shields.io/badge/TypeScript-5.8-3178C6?logo=typescript&logoColor=white)

Terrarium Tavern 是基于 [Kohaku Terrarium](https://github.com/Kohaku-Lab/KohakuTerrarium) 的本地角色扮演应用。它围绕长篇互动叙事组织角色卡、世界书、对话分支和剧情记忆，探索如何在有限上下文中维持设定与故事的连续性。

项目目前处于实验阶段，面向 Windows 本机使用。支持文本角色卡，以及带 HTML 界面和 MVU 变量的角色卡；模型通过兼容 OpenAI 的 API 接入，前台写作与后台整理可以使用不同模型。

## 核心设计

### 在对话中途注入世界书

世界书采用 **mid-conversation system prompt** 的注入方式：把检索到的相关设定作为系统消息，放在对话历史中与其对应的位置。

条目与触发它的剧情进度关联，在上下文中保持位置，不因重复命中不断上移或复制。随着对应交流退出上下文，条目一同退出并进入冷却。应用也提供集中置顶的注入模式，以适应不同模型和使用习惯。

这让世界书注入具有明确的时机与生命周期。能否充分发挥作用，仍取决于模型及服务商对中途系统消息的支持。

### 多路世界书索引与检索

世界书检索结合 **关键词匹配、BM25 全文检索和可选的向量检索**，并可使用重排模型进一步筛选。默认的关键词与 BM25 路线不需要 GPU；向量检索和重排可使用本地 Qwen3 模型。

各路候选经过融合、相关性准入和预算检查，递归触发也遵循相同约束。数量限制是上限，允许零命中，避免为了填满上下文而注入弱相关设定。

### 周期性触发的积累式记忆

后台按周期整理已提交剧情，默认每五轮触发一次；历史即将退出上下文时，还会检查相关剧情是否已经完成记忆整理。

记忆采用增量操作，逐步积累当前进展、变化与经历、未完成事项和待确认内容。已有条目不会仅因本轮没有被提到就消失，手工修订受到保护。模型负责判断事实与变化，程序负责回填原文来源、校验引用并提交状态。用户可以查看来源、编辑条目或手动重建记忆。

## 技术路线

前端使用 **Vue 3、TypeScript 和 Vite**；后端使用 **Python、FastAPI 和 SQLite**，以 Kohaku Terrarium 作为智能体运行基座，借助隔离的 Chromium 环境运行角色卡脚本。

- **前台写作（Writer）**：负责叙事，普通正文生成不调用工具。
- **后台整理（Tracker）**：负责积累式记忆、按需变量分析，以及经用户授权的设定维护。
- **应用运行时**：负责检索、上下文组装、分支、权限和事务提交。模型输出与脚本处理成功后，正文和状态才一起生效。

对话和配置保存在本机，应用仅监听本地地址。调用远程模型时，相应的角色资料、设定和剧情上下文仍会发送给所配置的服务商。

## 安装

准备好 **Python、Node.js 22+ 和 Git**。项目运行环境固定为 Python 3.12，安装脚本通过 uv 管理依赖和所需的 Python 版本。

克隆本仓库，或下载并解压源码后，在项目根目录打开 PowerShell：

```powershell
.\setup.ps1
.\start.ps1
```

安装脚本会安装锁定依赖、构建前端并下载 Chromium。启动后访问 [http://127.0.0.1:8000](http://127.0.0.1:8000)。

安装时默认直连。如需代理，可显式指定：

```powershell
.\setup.ps1 -Proxy 'http://127.0.0.1:你的代理端口'
```

模型 API 的代理在应用设置中单独配置。若需本地向量检索和重排，使用 `setup.ps1 -LocalRetrieval` 安装额外依赖，再在应用设置中下载并预热模型；这条路线目前需要支持 CUDA 的 NVIDIA GPU。

停止服务：

```powershell
.\stop.ps1
```

自定义端口使用 `start.ps1 -Port 8001`，自定义数据目录使用 `start.ps1 -Data 'D:\MyStories'`。详细说明见[安装与维护](docs/guides/maintenance.md)。

## 开始使用

1. **配置模型**：在设置中添加 API 提供商，为前台写作和后台整理选择模型，并检查接口能力。后台历史补查等功能需要模型支持工具调用。
2. **导入角色卡**：可以先试用 `samples/雾港来信.json`；`samples/星航状态卡.json` 用于体验 HTML/MVU，使用前需启用该卡的脚本权限。
3. **开始故事**：选择角色卡、玩家身份和开场，输入行动或对白。需要更新变量或修改设定时，按界面提示明确授权。
4. **查看与调整**：通过上下文预览查看注入内容，通过后台记录了解整理过程；可编辑记忆、重新生成回复，或从选定进度创建独立分支。

数据默认位于 `data/`，其中包含私人剧情、角色资源和模型配置。备份也可能包含密钥，请自行妥善保管；不要把数据目录或备份提交到仓库。

## 当前局限与未完部分

- **角色卡编辑智能体仍比较粗糙。** 已提供草稿编辑、工作流适配、校验和保存流程，但交互打磨、复杂卡片理解及修改质量仍需完善。请人工审阅修改结果，尤其是初始化、脚本与世界书用途的调整。
- **角色卡兼容范围有限。** 项目不是完整的 SillyTavern 运行环境，不能保证任意扩展、远程脚本或复杂 MVU 卡片都能直接使用，详见[兼容范围](docs/guides/compatibility.md)。
- **长篇叙事质量仍在探索。** 检索阈值、记忆取舍和模型指令遵循会影响实际体验；工程回归通过不等于模型一定理解正确，也不保证所有角色卡的长期一致性。
- **模型与服务商存在差异。** 中途系统消息、工具调用和推理参数的支持程度不同；本地 token 估算也不等于服务商的实际计费量。
- **交付形态尚不完善。** 当前以 Windows 源码运行流程为主，没有图形安装器，其他平台尚未完成适配验证，也不面向公网多人部署。
- **历史编辑有边界。** 修改原文会标记相关记忆来源发生变化，不会自动重演后续剧情或变量。需要同步改变后续进度时，应从合适位置建立分支或重新生成。

## 文档与开发

- [文档导航](docs/README.md)
- [架构与职责](docs/design/architecture.md) · [上下文生命周期](docs/design/context-lifecycle.md)
- [世界书检索](docs/guides/retrieval.md) · [积累式记忆](docs/design/memory-consistency.md)
- [角色卡编辑](docs/guides/card-editor.md) · [项目目录](docs/development/structure.md)
- [验证与发行](docs/development/validation.md) · [开发约定](.agents/AGENTS.md)

常用开发命令：

```powershell
npm.cmd run dev
npm.cmd run build
npm.cmd test
.venv/Scripts/python.exe -m pytest -q
```

## 致谢

- [Kohaku Terrarium](https://github.com/Kohaku-Lab/KohakuTerrarium)：提供智能体运行基座，是本项目的重要基础。
- [SillyTavern](https://github.com/SillyTavern/SillyTavern)：角色卡、世界书和互动叙事工作流的重要灵感来源。
- [MagVarUpdate](https://github.com/MagicalAstrogy/MagVarUpdate)：提供 MVU 变量更新核心。
- [Tavern Helper](https://github.com/n0vi028/JS-Slash-Runner)：为角色卡脚本接口适配提供参考。
- 角色卡规范、Vue、FastAPI、Playwright、tiktoken 等项目及其贡献者。

Powered by **KohakuTerrarium**。Copyright 2024–2026 Shih-Ying Yeh (KohakuBlueLeaf) and contributors。

第三方组件的来源、固定版本与许可说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
