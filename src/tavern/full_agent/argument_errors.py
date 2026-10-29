"""Full Agent's result envelope for shared tool argument feedback."""

from tavern.core.tool_arguments import argument_feedback
from tavern.full_agent.protocol import ToolError


class ToolArgumentError(ToolError):
    def __init__(self, name, model, error):
        feedback = argument_feedback(model, error)
        message = f"{name} 参数错误，工具未执行：{feedback['message']}。"
        if "expected_parameters" in feedback:
            message += "请参照 expected_parameters 中的相关结构修正后重试。"
        super().__init__("INVALID_ARGUMENTS", message)
        if "expected_parameters" in feedback:
            self.result["expected_parameters"] = feedback["expected_parameters"]
