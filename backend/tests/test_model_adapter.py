import httpx

from app.agent.model_adapter import FakeModelAdapter, OpenAICompatAdapter


def test_fake_adapter_returns_scripted_steps():
    adapter = FakeModelAdapter(script=[
        {"type": "tool_call", "tool_name": "http_get", "arguments": {"url": "http://x"}},
        {"type": "final_answer", "content": "done"},
    ])
    assert adapter.complete([], [])["type"] == "tool_call"
    assert adapter.complete([], [])["type"] == "final_answer"


def test_openai_compat_adapter_parses_tool_call(monkeypatch):
    captured_request = {}

    def fake_post(self, url, headers=None, json=None):
        captured_request["url"] = url
        captured_request["json"] = json
        body = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "http_get",
                                    "arguments": '{"url": "http://x"}',
                                }
                            }
                        ]
                    }
                }
            ]
        }
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post.__get__(httpx, type(httpx)))

    adapter = OpenAICompatAdapter(base_url="https://api.example.com/v1", api_key="k", model_name="m")
    tools = [{"name": "http_get", "description": "fetch a url", "parameters": {}}]
    step = adapter.complete([{"role": "user", "content": "go"}], tools)

    assert captured_request["url"] == "https://api.example.com/v1/chat/completions"
    assert captured_request["json"]["tools"] == [
        {"type": "function", "function": {"name": "http_get", "description": "fetch a url", "parameters": {}}}
    ]
    assert step == {"type": "tool_call", "tool_name": "http_get", "arguments": {"url": "http://x"}}


def test_openai_compat_adapter_returns_final_answer_when_no_tool_calls(monkeypatch):
    def fake_post(self, url, headers=None, json=None):
        body = {"choices": [{"message": {"content": "the answer is 4"}}]}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post.__get__(httpx, type(httpx)))

    adapter = OpenAICompatAdapter(base_url="https://api.example.com/v1", api_key="k", model_name="m")
    step = adapter.complete([{"role": "user", "content": "what is 2+2?"}], [])

    assert step == {"type": "final_answer", "content": "the answer is 4"}


def test_openai_compat_adapter_returns_final_answer_with_empty_string_when_content_is_null(monkeypatch):
    def fake_post(self, url, headers=None, json=None):
        body = {"choices": [{"message": {"content": None}}]}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post.__get__(httpx, type(httpx)))

    adapter = OpenAICompatAdapter(base_url="https://api.example.com/v1", api_key="k", model_name="m")
    step = adapter.complete([], [])

    assert step == {"type": "final_answer", "content": ""}


def test_openai_compat_adapter_returns_malformed_tool_call_on_json_parse_failure(monkeypatch):
    def fake_post(self, url, headers=None, json=None):
        body = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"function": {"name": "http_get", "arguments": "{not valid json"}}
                        ]
                    }
                }
            ]
        }
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post.__get__(httpx, type(httpx)))

    adapter = OpenAICompatAdapter(base_url="https://api.example.com/v1", api_key="k", model_name="m")
    step = adapter.complete([], [])

    assert step["type"] == "malformed_tool_call"
    assert step["raw"] == "{not valid json"
