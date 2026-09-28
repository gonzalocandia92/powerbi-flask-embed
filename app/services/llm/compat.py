"""Legacy input conversion at the boundary, never the canonical history format."""
from .contracts import LLMMessage, ToolCall, ToolResult


def normalize_history(messages):
    result = []
    for message in messages:
        if isinstance(message, LLMMessage):
            result.append(message)
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            result.append(LLMMessage(message["role"], content))
            continue
        text, calls, results = [], [], []
        for block in content or []:
            if block.get("type") == "text":
                text.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(ToolCall(block["id"], block["name"], block.get("input") or {}))
            elif block.get("type") == "tool_result":
                results.append(ToolResult(block["tool_use_id"], block.get("content", "")))
        result.append(LLMMessage(message["role"], "\n".join(text), calls, results))
    while result and result[0].tool_results and not result[0].text and not result[0].tool_calls:
        result.pop(0)
    return result
