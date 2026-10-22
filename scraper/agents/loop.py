"""The tool-calling loop every agent runs on.

An agent works through typed tools. Results it builds up (materials, plan items)
live in Python objects behind the tools, not in its chat history, and it ends
by calling its one *final* tool (dispatch, finish, submit_review, ...). The
loop enforces the step limit, the token budget and cancellation, and narrates
what the agent says and does.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import openai
import requests
from pydantic import BaseModel, ValidationError

from ..config import (
    AGENT_MAX_STEPS,
    AGENT_MAX_TOKENS,
    AGENT_TOOL_RESULT_CHARS,
    CONTEXT_KEEP_LARGE_RESULTS,
    CONTEXT_LARGE_RESULT_CHARS,
    LLM_CALL_RETRIES,
)
from ..run_context import RunContext, emit
from .policy import TOOL_POLICY, UNTRUSTED_CONTENT

log = logging.getLogger(__name__)

_THINK = re.compile(r"<think(?:ing)?>.*?(?:</think(?:ing)?>|$)", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_RETRYABLE = (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError, openai.InternalServerError,
              requests.ConnectionError, requests.Timeout)


@dataclass
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    fn: Callable[[Any], Any]
    final: bool = False  # calling it successfully ends the agent's run

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description,
                         "parameters": _strip_titles(_inline_refs(self.args.model_json_schema()))},
        }


class ToolError(Exception):
    """Raised by a tool to send a corrective message back to the agent."""


@dataclass
class AgentResult:
    finished: bool  # the final tool was called successfully
    final: Any  # what the final tool returned
    steps: int
    tokens: int
    stopped: str = ""  # "", "step_limit", "token_limit", "cancelled", "budget", "no_tool_calls"


def _inline_refs(schema: dict) -> dict:
    """Replace "$ref": "#/$defs/X" with the definition itself; not every server resolves refs."""
    defs = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node and node["$ref"].startswith("#/$defs/"):
                target = resolve(defs[node["$ref"].split("/")[-1]])
                return {**target, **{k: resolve(v) for k, v in node.items() if k != "$ref"}}
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


def _strip_titles(schema: Any) -> Any:
    """Pydantic adds a "title" to every schema node; models don't need them."""
    if isinstance(schema, dict):
        return {k: _strip_titles(v) for k, v in schema.items()
                if not (k == "title" and isinstance(v, str))}
    if isinstance(schema, list):
        return [_strip_titles(v) for v in schema]
    return schema


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n…[truncated, {len(text)} chars in total]"


def _render(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, BaseModel):
        return result.model_dump_json(exclude_defaults=True)
    return json.dumps(result, ensure_ascii=False, default=str)


def run_agent(
    *,
    role: str,
    name: str,
    system: str,
    user: str,
    tools: list[Tool],
    rc: RunContext,
    max_steps: int | None = None,
) -> AgentResult:
    if extra := sorted({tool.name for tool in tools} - TOOL_POLICY[role]):
        raise PermissionError(f"the {role} role may not use {extra} (see agents/policy.py)")
    system = f"{system}\n\n{UNTRUSTED_CONTENT}"
    max_steps = max_steps or AGENT_MAX_STEPS[role]
    by_name = {tool.name: tool for tool in tools}
    finals = [tool for tool in tools if tool.final]
    schemas = [tool.schema() for tool in tools]
    client = rc.llm.client(role)
    messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    tokens = 0
    nudges = 0

    token_cap = AGENT_MAX_TOKENS[role]
    for step in range(1, max_steps + 1):
        if reason := rc.stop_reason():
            return AgentResult(False, None, step - 1, tokens, stopped=reason)
        if tokens >= token_cap:
            emit({"type": "narration", "role": role, "agent": name,
                  "text": f"Stopping: reached my token cap ({token_cap:,})."})
            return AgentResult(False, None, step - 1, tokens, stopped="token_limit")

        mask_old_results(messages)
        turn = _complete(client, messages, schemas)
        used = turn.tokens or sum(len(str(m.get("content") or "")) for m in messages) // 4
        tokens += used
        rc.budget.charge(role, used)

        said = _THINK.sub("", turn.content).strip()
        if said and turn.tool_calls:
            emit({"type": "narration", "role": role, "agent": name, "text": _clip(said, 600)})

        assistant: dict = {"role": "assistant", "content": turn.content or ""}
        if turn.tool_calls:
            assistant["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in turn.tool_calls
            ]
        messages.append(assistant)

        if not turn.tool_calls:
            # Weaker models sometimes write the final call as JSON text instead of a tool call.
            if len(finals) == 1 and (result := _final_from_text(finals[0], said)) is not None:
                return AgentResult(True, result, step, tokens)
            nudges += 1
            if nudges > 2:
                return AgentResult(False, None, step, tokens, stopped="no_tool_calls")
            names = ", ".join(f"`{t.name}`" for t in finals)
            messages.append({"role": "user", "content": f"Continue using the tools. When you are done, call {names}."})
            continue

        done, final_result = False, None
        for call in turn.tool_calls:
            tool = by_name.get(call.name)
            if tool is None:
                content = f"ERROR: unknown tool {call.name!r}. Available: {', '.join(by_name)}"
            else:
                ok, content, result = _call(tool, call.arguments)
                emit({"type": "tool", "role": role, "agent": name, "tool": call.name, "ok": ok})
                if ok and tool.final:
                    done, final_result = True, result
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": _clip(content, AGENT_TOOL_RESULT_CHARS)})
        if done:
            return AgentResult(True, final_result, step, tokens)

    return AgentResult(False, None, max_steps, tokens, stopped="step_limit")


MASKED = ("[earlier {tool} result removed to save context ({chars:,} chars){what}. Its ids stay valid; "
          "call {tool} again with the same arguments to see it again]")
_LISTING_HEADER = re.compile(r"^links \d+-\d+ of \d+", re.MULTILINE)


def _summary(content: str) -> str:
    """What a masked result was, e.g. ': PAGE https://c.edu/fall2025, links 1-45 of 99'."""
    first = content.split("\n", 1)[0][:160]
    header = _LISTING_HEADER.search(content)
    return f": {first}" + (f", {header.group(0)}" if header else "")


def mask_old_results(messages: list[dict], keep: int = CONTEXT_KEEP_LARGE_RESULTS,
                     min_chars: int = CONTEXT_LARGE_RESULT_CHARS) -> int:
    """Replace all but the newest `keep` large tool results with a short stub. Returns how many.

    Every call re-sends the whole conversation, so without this a Scout's tenth step pays
    again for every page listing it has already worked through: cost grows quadratically.
    Stubs replace results for good, so the conversation's earlier part stays stable.
    """
    names = {c["id"]: c["function"]["name"] for m in messages if m["role"] == "assistant"
             for c in m.get("tool_calls") or []}
    # A stub is far shorter than min_chars, so masked results never count as large again.
    large = [m for m in messages if m["role"] == "tool" and len(m.get("content") or "") >= min_chars]
    old = large[:-keep] if keep else large
    for m in old:
        m["content"] = MASKED.format(tool=names.get(m.get("tool_call_id"), "tool"), chars=len(m["content"]),
                                     what=_summary(m["content"]))
    return len(old)


def _complete(client, messages: list[dict], schemas: list[dict]):
    for attempt in range(LLM_CALL_RETRIES + 1):
        try:
            return client.complete(messages, schemas)
        except _RETRYABLE as exc:
            if attempt == LLM_CALL_RETRIES:
                raise
            wait = 5 * (attempt + 1)
            log.warning("LLM call failed (%s); retrying in %ss", type(exc).__name__, wait)
            time.sleep(wait)
    raise AssertionError("unreachable")


def _call(tool: Tool, raw_args: str) -> tuple[bool, str, Any]:
    try:
        args = tool.args.model_validate(json.loads(raw_args or "{}"))
    except json.JSONDecodeError as exc:
        return False, f"ERROR: arguments are not valid JSON ({exc}). Call {tool.name} again.", None
    except ValidationError as exc:
        return False, f"ERROR: invalid arguments for {tool.name}:\n{_brief_errors(exc)}", None
    try:
        result = tool.fn(args)
    except ToolError as exc:
        return False, f"ERROR: {exc}", None
    except Exception as exc:  # noqa: BLE001 - report to the agent, keep going
        log.exception("tool %s failed", tool.name)
        return False, f"ERROR: {type(exc).__name__}: {exc}", None
    return True, _render(result), result


def _brief_errors(exc: ValidationError) -> str:
    return "\n".join(f"- {'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:10])


def _final_from_text(tool: Tool, text: str) -> Any | None:
    fence = _FENCE.search(text)
    candidate = fence.group(1) if fence else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end <= start:
        return None
    ok, _content, result = _call(tool, candidate[start:end + 1])
    return result if ok else None
