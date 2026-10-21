import json
from typing import Protocol, TypedDict

import httpx


class ToolCallStep(TypedDict):
    type: str  # "tool_call"
    tool_name: str
    arguments: dict


class FinalAnswerStep(TypedDict):
    type: str  # "final_answer"
    content: str


class MalformedToolCallStep(TypedDict):
    type: str  # "malformed_tool_call"
    raw: str


ModelStep = ToolCallStep | FinalAnswerStep | MalformedToolCallStep


class ModelAdapter(Protocol):
    def complete(self, messages: list[dict], tools: list[dict]) -> ModelStep: ...


class FakeModelAdapter:
    """Test double that returns each scripted step in order."""

    def __init__(self, script: list[ModelStep]):
        self._script = list(script)
        self._index = 0

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelStep:
        step = self._script[self._index]
        self._index += 1
        return step


class OpenAICompatAdapter:
    def __init__(self, base_url: str, api_key: str, model_name: str):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model_name = model_name

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelStep:
        wire_tools = [{"type": "function", "function": tool} for tool in tools]
        response = httpx.post(
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={"model": self._model_name, "messages": messages, "tools": wire_tools},
        )
        response.raise_for_status()
        message = response.json()["choices"][0]["message"]

        tool_calls = message.get("tool_calls")
        if not tool_calls:
            return {"type": "final_answer", "content": message.get("content") or ""}

        call = tool_calls[0]["function"]
        raw_arguments = call["arguments"]
        try:
            arguments = json.loads(raw_arguments)
        except (json.JSONDecodeError, TypeError):
            return {"type": "malformed_tool_call", "raw": raw_arguments}

        return {"type": "tool_call", "tool_name": call["name"], "arguments": arguments}

