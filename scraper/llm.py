"""Switchable LLM backends: one OpenAI-compatible chat client per agent role.

HF router, a local Ollama, or any other OpenAI-compatible server can back any
role. Tests plug in scripted clients through the same ChatClient protocol.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import requests

from .config import AGENT_MODELS, LLM_ENDPOINTS, LLM_MAX_OUTPUT_TOKENS, LLM_TEMPERATURE


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON text, as the model wrote it


@dataclass(frozen=True)
class Turn:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens: int = 0  # prompt + completion, as reported by the server (0 if unknown)
    reasoning: str = ""  # separate reasoning channel, for models that expose one


class ChatClient(Protocol):
    model: str

    def complete(self, messages: list[dict], tools: list[dict]) -> Turn: ...


class OpenAICompatClient:
    def __init__(self, base_url: str, api_key: str, model: str,
                 temperature: float = LLM_TEMPERATURE, max_tokens: int = LLM_MAX_OUTPUT_TOKENS) -> None:
        from openai import OpenAI

        self.model = model
        self._client = OpenAI(base_url=base_url, api_key=api_key, max_retries=2, timeout=300)
        self._temperature = temperature
        self._max_tokens = max_tokens

    def complete(self, messages: list[dict], tools: list[dict]) -> Turn:
        kwargs: dict = {"model": self.model, "messages": messages,
                        "temperature": self._temperature, "max_tokens": self._max_tokens}
        if tools:
            kwargs.update(tools=tools, tool_choice="auto")
        resp = self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        extra = getattr(msg, "model_extra", None) or {}
        return Turn(
            content=msg.content or "",
            tool_calls=[ToolCall(tc.id, tc.function.name, tc.function.arguments or "{}") for tc in msg.tool_calls or []],
            tokens=resp.usage.total_tokens if resp.usage else 0,
            reasoning=extra.get("reasoning_content") or extra.get("reasoning") or "",
        )


class OllamaClient:
    """Ollama's native chat API.

    Unlike Ollama's OpenAI-compatible endpoint, it lets us set the context window
    (the default is small and silently truncates long prompts) and switch the
    model's thinking off, which matters a lot on a CPU.
    """

    def __init__(self, base_url: str, model: str, *, num_ctx: int = 32768, think: bool = False,
                 temperature: float = LLM_TEMPERATURE, max_tokens: int = LLM_MAX_OUTPUT_TOKENS,
                 timeout: float = 1800) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._url = self.base_url + "/api/chat"
        self._options = {"num_ctx": num_ctx, "temperature": temperature, "num_predict": max_tokens}
        self._think = think
        self._timeout = timeout

    def complete(self, messages: list[dict], tools: list[dict]) -> Turn:
        body: dict = {"model": self.model, "messages": to_ollama_messages(messages), "stream": False,
                      "think": self._think, "options": self._options, "keep_alive": "15m"}
        if tools:
            body["tools"] = tools
        resp = requests.post(self._url, json=body, timeout=self._timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"Ollama error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        msg = data.get("message") or {}
        calls = []
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            args = fn.get("arguments") or {}
            calls.append(ToolCall(call.get("id") or f"call_{uuid.uuid4().hex[:8]}", fn.get("name", ""),
                                  args if isinstance(args, str) else json.dumps(args)))
        return Turn(content=msg.get("content") or "", tool_calls=calls,
                    tokens=(data.get("prompt_eval_count") or 0) + (data.get("eval_count") or 0),
                    reasoning=msg.get("thinking") or "")


def ollama_running(base_url: str) -> bool:
    try:
        return requests.get(base_url.rstrip("/") + "/api/version", timeout=3).ok
    except requests.RequestException:
        return False


def to_ollama_messages(messages: list[dict]) -> list[dict]:
    """OpenAI-style messages -> Ollama's: tool-call arguments as objects, tool results by tool name."""
    names: dict[str, str] = {}
    native = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            calls = []
            for c in m["tool_calls"]:
                names[c["id"]] = c["function"]["name"]
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                calls.append({"function": {"name": c["function"]["name"], "arguments": args}})
            native.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
        elif m["role"] == "tool":
            native.append({"role": "tool", "content": m.get("content") or "",
                           "tool_name": names.get(m.get("tool_call_id", ""), "")})
        else:
            native.append({"role": m["role"], "content": m.get("content") or ""})
    return native


def _hf_token_file() -> str | None:
    path = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "token"
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def resolve_api_key(endpoint: str, provided: dict[str, str] | None = None) -> str:
    if provided and provided.get(endpoint):
        return provided[endpoint]
    settings = LLM_ENDPOINTS[endpoint]
    if settings.get("api_key"):
        return settings["api_key"]
    if env := settings.get("api_key_env"):
        if os.environ.get(env):
            return os.environ[env]
    if endpoint == "hf" and (token := _hf_token_file()):
        return token
    raise ValueError(f"No API key for the {endpoint!r} endpoint; pass one or set {settings.get('api_key_env')}")


class LLMRouter:
    """Hands each agent role its own client."""

    def __init__(self, clients: dict[str, ChatClient]) -> None:
        self._clients = clients

    def client(self, role: str) -> ChatClient:
        return self._clients[role]

    def describe(self) -> dict[str, str]:
        return {role: client.model for role, client in self._clients.items()}

    def ollama_urls(self) -> set[str]:
        return {c.base_url for c in self._clients.values() if isinstance(c, OllamaClient)}

    @classmethod
    def from_config(cls, api_keys: dict[str, str] | None = None,
                    models: dict[str, dict] | None = None) -> LLMRouter:
        models = {**AGENT_MODELS, **(models or {})}
        clients: dict[str, ChatClient] = {}
        for role, spec in models.items():
            endpoint = spec["endpoint"]
            settings = LLM_ENDPOINTS[endpoint]
            base_url = spec.get("base_url") or settings["base_url"]
            if settings.get("api") == "ollama":
                clients[role] = OllamaClient(base_url, spec["model"], num_ctx=settings.get("num_ctx", 32768),
                                             think=settings.get("think", False))
            else:
                clients[role] = OpenAICompatClient(base_url=base_url, api_key=resolve_api_key(endpoint, api_keys),
                                                   model=spec["model"])
        return cls(clients)
