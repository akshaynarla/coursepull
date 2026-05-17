"""Generic chat-with-tools agent loop. One runtime, many agents."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from openai import OpenAI

from ..config import (
    HF_BASE_URL,
    HF_MODEL,
    LLM_MAX_OUTPUT_TOKENS,
    LLM_TEMPERATURE,
    TOOL_RESULT_CHAR_CAP,
)

log = logging.getLogger(__name__)


ToolDispatch = Callable[[str, dict[str, Any]], Any]
LogFn = Callable[[str], None]


@dataclass
class AgentResult:
    final_message: str
    steps: int
    stopped_for: str  # "final_answer" | "step_cap" | "no_response"


class AgentRunner:
    """Drives a multi-step tool-calling loop against the HF Inference API."""

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("HuggingFace token is required.")
        self.client = OpenAI(base_url=HF_BASE_URL, api_key=api_key)

    def run(
        self,
        agent_name: str,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        dispatch: ToolDispatch,
        max_steps: int,
        log_callback: LogFn,
    ) -> AgentResult:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        for step in range(1, max_steps + 1):
            log.debug("[%s] step %d → calling LLM with %d messages", agent_name, step, len(messages))
            resp = self.client.chat.completions.create(
                model=HF_MODEL,
                messages=messages,
                tools=tools if tools else None,
                tool_choice="auto" if tools else None,
                temperature=LLM_TEMPERATURE,
                max_tokens=LLM_MAX_OUTPUT_TOKENS,
            )
            msg = resp.choices[0].message
            log.debug("[%s] step %d ← content=%r tool_calls=%s",
                      agent_name, step, msg.content, bool(msg.tool_calls))

            assistant_msg: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                        },
                    }
                    for tc in msg.tool_calls
                ]
            messages.append(assistant_msg)

            if not msg.tool_calls:
                log_callback(f"  [{agent_name} step {step}] returned final answer ({len(msg.content or '')} chars)")
                return AgentResult(final_message=msg.content or "", steps=step, stopped_for="final_answer")

            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError as exc:
                    args = {}
                    tool_result: Any = f"ERROR: invalid JSON arguments: {exc}"
                else:
                    log_callback(f"  [{agent_name} step {step}] tool: {name}({_short_args(args)})")
                    try:
                        tool_result = dispatch(name, args)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("[%s] tool %s failed", agent_name, name)
                        tool_result = f"ERROR: {type(exc).__name__}: {exc}"

                content = tool_result if isinstance(tool_result, str) else json.dumps(tool_result, ensure_ascii=False)
                if len(content) > TOOL_RESULT_CHAR_CAP:
                    content = content[:TOOL_RESULT_CHAR_CAP] + f"\n...[truncated, full {len(content)} chars]"
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})

        log_callback(f"  [{agent_name}] step cap ({max_steps}) reached")
        return AgentResult(
            final_message=messages[-1].get("content", "") if messages else "",
            steps=max_steps,
            stopped_for="step_cap",
        )


def _short_args(args: dict[str, Any], limit: int = 120) -> str:
    s = json.dumps(args, ensure_ascii=False)
    return s if len(s) <= limit else s[: limit - 3] + "..."
