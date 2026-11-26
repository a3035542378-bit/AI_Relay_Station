from copy import deepcopy
import sys
from types import ModuleType
from unittest.mock import Mock

import pytest

from personal_protocol import prepare_request


OFFICIAL = "https://api.openai.com/v1"


@pytest.mark.parametrize("model", ["gpt-6.2-sol", "gpt-6.2-astra"])
def test_official_chat_parameters(model):
    request = {
        "messages": [{"role": "user", "content": "test"}], "temperature": 0.1, "top_p": 0.2,
        "max_tokens": 48, "reasoning_effort": "minimal", "logprobs": False, "top_logprobs": 2,
        "stream_options": {"include_usage": True}, "metadata": {"private": True},
        "tools": [], "tool_choice": "none", "parallel_tool_calls": False,
    }
    original = deepcopy(request)
    route, payload = prepare_request(OFFICIAL, model, request)
    assert route == "/chat/completions"
    assert payload == {"model": model, "messages": request["messages"], "max_completion_tokens": 48, "reasoning_effort": "low"}
    assert request == original


@pytest.mark.parametrize(("endpoint", "model"), [
    ("https://api.openai.com.other.example/v1", "gpt-6.2-sol"),
    ("https://api.openai.com:445/v1", "gpt-6.2-sol"),
    ("https://api.openai.com/v1/proxy", "gpt-6.2-sol"),
    (OFFICIAL, "custom-gpt-6.2-sol"),
])
def test_other_providers_and_models_keep_their_original_protocol(endpoint, model):
    tool = {"type": "function", "function": {"name": "lookup"}}
    request = {"messages": [], "tools": [tool], "reasoning_effort": "none", "temperature": 0.3, "max_tokens": 40}
    assert prepare_request(endpoint, model, {**request, "model": "personal:id", "metadata": {"secret": "local"}}) == (
        "/chat/completions", {**request, "model": model}
    )


@pytest.fixture
def webui_converter(monkeypatch):
    def convert(payload):
        # 模拟原生转换器消费输入，检查调用方不会修改聊天记录。
        payload["input"] = payload.pop("messages")
        payload["input"].append({"role": "user", "content": "test sentinel"})
        if "max_completion_tokens" in payload:
            payload["max_output_tokens"] = payload.pop("max_completion_tokens")
        return payload

    module = ModuleType("open_webui.routers.openai")
    module.convert_to_responses_payload = Mock(side_effect=convert)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module.convert_to_responses_payload


@pytest.mark.parametrize("context", [
    {"tools": [{"type": "function", "function": {"name": "lookup"}}]},
    {"messages": [{"role": "tool", "tool_call_id": "c1", "content": "ok"}]},
    {"messages": [{"role": "assistant", "tool_calls": [{"id": "c1"}]}]},
    {"messages": [{"role": "assistant", "output": [{"type": "function_call"}]}]},
    {"messages": [{"role": "assistant", "reasoning_items": [{"type": "reasoning"}]}]},
])
def test_tool_and_history_use_stateless_responses(context, webui_converter):
    schema = {"name": "test", "schema": {"type": "object"}, "strict": True}
    request = {
        "messages": [], **context, "reasoning_effort": "none", "max_tokens": 12, "max_completion_tokens": 96,
        "response_format": {"type": "json_schema", "json_schema": schema}, "stream": True, "temperature": 1,
        "seed": 123, "store": True, "include": ["message.output_text.logprobs"],
    }
    original = deepcopy(request)
    route, payload = prepare_request(OFFICIAL, "gpt-6.2-sol", request)
    assert route == "/responses" and request == original
    webui_converter.assert_called_once()
    assert payload["max_output_tokens"] == 96
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["text"]["format"] == {**schema, "type": "json_schema"}
    assert payload["include"] == ["reasoning.encrypted_content"] and payload["store"] is False
    assert payload["stream"] is True
    assert {"reasoning_effort", "response_format", "temperature", "seed", "max_tokens"}.isdisjoint(payload)
