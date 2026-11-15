"""Application-owned prompt defaults and settings catalog."""

from tavern.story.config import STORY_PROMPT

DEFAULT_PREFERENCE_PROMPT = """以下是当前用户指定的特别创作偏好："""


DEFAULT_REGENERATE_PROMPT = """以下创作要求仅适用于本次回复："""

DEFAULT_LOCAL_INSTRUCTION = """Retrieve worldbook passages relevant to the current scene, characters, places, events, and unresolved matters."""

DEFAULT_RERANKER_PROMPT = """Judge whether the Document is relevant to the Query under the given Instruct. Answer with exactly one word: yes or no."""

DEFAULT_PROBE_PROMPT = """Follow the latest scene marker provided in a system message."""

DEFAULT_PROBE_MARKER_PROMPT = (
    """The current scene marker is TAVERN_MID_42. Reply with exactly TAVERN_MID_42."""
)

STORY_SYSTEM_PROMPT_DEFINITION = {
    "path": "system_prompt",
    "label": "顶级规则/引导",
    "description": "剧情叙事与连续性原则；工具授权、记录职责和提交规则由运行时协议提供，剧情规划原则也在开启时由协议注入；具体参数与操作条件见工具说明。",
    "default": STORY_PROMPT,
}
PROMPT_DEFINITIONS = [
    {
        "path": "preference_prompt",
        "label": "对话偏好引导词",
        "description": "程序在此文字后附加当前对话的创作偏好。",
        "default": DEFAULT_PREFERENCE_PROMPT,
    },
    {
        "path": "regenerate_prompt",
        "label": "重新生成引导词",
        "description": "程序在此文字后附加本次重新生成的补充要求。",
        "default": DEFAULT_REGENERATE_PROMPT,
    },
    {
        "path": "local_runtime.instruction_prompt",
        "label": "本地检索任务提示词",
        "description": "供 Embedding 查询和 Reranker 共用；保存后用于后续检索。",
        "default": DEFAULT_LOCAL_INSTRUCTION,
    },
    {
        "path": "local_runtime.reranker_prompt",
        "label": "Reranker 判断提示词",
        "description": "保留 yes/no 输出约定，模型通过对应 token 的概率计算得分。",
        "default": DEFAULT_RERANKER_PROMPT,
    },
    {
        "path": "probe_system_prompt",
        "label": "接口检查引导词",
        "description": "用于测试接口对中途系统消息的支持。",
        "default": DEFAULT_PROBE_PROMPT,
    },
    {
        "path": "probe_marker_prompt",
        "label": "接口检查标记提示词",
        "description": "保留 TAVERN_MID_42，程序用该标记检查响应。",
        "default": DEFAULT_PROBE_MARKER_PROMPT,
    },
]
