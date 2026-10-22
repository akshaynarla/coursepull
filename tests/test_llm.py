from __future__ import annotations

import json

from scraper.llm import LLMRouter, OllamaClient, to_ollama_messages


def test_openai_style_messages_become_ollama_native():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "fetch_page", "arguments": '{"url": "https://x"}'}},
            {"id": "c2", "type": "function", "function": {"name": "inspect_url", "arguments": "not json"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "PAGE https://x"},
        {"role": "tool", "tool_call_id": "c2", "content": "{}"},
    ]
    native = to_ollama_messages(messages)
    assert native[2]["tool_calls"] == [
        {"function": {"name": "fetch_page", "arguments": {"url": "https://x"}}},
        {"function": {"name": "inspect_url", "arguments": {}}},
    ]
    assert native[3] == {"role": "tool", "content": "PAGE https://x", "tool_name": "fetch_page"}
    assert native[4]["tool_name"] == "inspect_url"


def test_router_uses_the_native_ollama_client_without_any_api_key():
    router = LLMRouter.from_config(models={r: {"endpoint": "ollama", "model": "qwen3.5:4b"}
                                           for r in ("orchestrator", "scout", "tutor", "reviewer", "examiner")})
    client = router.client("scout")
    assert isinstance(client, OllamaClient) and client.model == "qwen3.5:4b"


def test_ollama_reply_is_parsed_into_a_turn(monkeypatch):
    class Resp:
        status_code = 200

        def json(self):
            return {"message": {"content": "ok", "thinking": "hmm", "tool_calls": [
                {"function": {"name": "finish", "arguments": {"notes": "done"}}}]},
                "prompt_eval_count": 120, "eval_count": 30}

    sent = {}
    monkeypatch.setattr("scraper.llm.requests.post", lambda url, json, timeout: sent.update(json) or Resp())
    turn = OllamaClient("http://localhost:11434", "m", num_ctx=8192).complete(
        [{"role": "user", "content": "hi"}], [{"type": "function", "function": {"name": "finish"}}])
    assert sent["options"]["num_ctx"] == 8192 and sent["think"] is False and sent["tools"]
    assert turn.tokens == 150 and turn.reasoning == "hmm"
    assert turn.tool_calls[0].name == "finish" and json.loads(turn.tool_calls[0].arguments) == {"notes": "done"}
