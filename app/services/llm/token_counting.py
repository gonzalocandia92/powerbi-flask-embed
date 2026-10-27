"""Native counting remains isolated here; generation always uses LiteLLM."""
import json
from .contracts import LLMRequest
from .runtime import instruction_blocks


def native_payload(request: LLMRequest) -> dict:
    messages = []
    for message in request.messages:
        if message.tool_results:
            content = [{"type": "tool_result", "tool_use_id": r.call_id, "content": r.content} for r in message.tool_results]
            messages.append({"role": "user", "content": content})
        elif message.tool_calls:
            content = [{"type": "text", "text": message.text}] if message.text else []
            content.extend({"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in message.tool_calls)
            messages.append({"role": "assistant", "content": content})
        else:
            messages.append(message.public_dict())
    return {"model": request.model.physical_model, "system": instruction_blocks(request),
            "messages": messages, "tools": [{"name": t.name, "description": t.description,
                                             "input_schema": t.parameters} for t in request.tools]}


async def count_tokens(request: LLMRequest) -> int:
    payload = native_payload(request)
    if request.model.provider == "anthropic" and request.model.gateway == "direct":
        try:
            from anthropic import AsyncAnthropic
            async with AsyncAnthropic(api_key=request.model.api_key) as client:
                response = await client.messages.count_tokens(**payload)
                return int(response.input_tokens)
        except Exception:
            pass
    # Same fallback serialization and heuristic as the original main path.
    text = "".join(json.dumps(payload[key], ensure_ascii=False, default=str) for key in ("system", "messages", "tools"))
    return max(1, len(text) // 4)
