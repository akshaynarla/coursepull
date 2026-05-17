"""Verifier agent: inspects failed downloads and proposes alternatives."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from pydantic import TypeAdapter, ValidationError

from ..config import MAX_STEPS_VERIFIER
from ..models import Action, DownloadResult, PlanItem, Subfolder
from .runtime import AgentRunner
from .tools import tool_inspect_url

log = logging.getLogger(__name__)


SYSTEM = """\
You are the Verifier agent. You are given a list of failed downloads from a course
scraper. For each failure, decide whether to RETRY with a different strategy or to
LEAVE IT ALONE (the original failure stands).

TOOLS:
- inspect_url(url): HEAD-peek; returns status, content-type, length, final URL.
- revise(url, action, chapter, subfolder, filename, reason): propose a new PlanItem
  to re-run for the given URL. Use this when inspect_url tells you the original
  action was wrong (e.g. download_pdf failed because the URL is actually a Google
  Doc → call revise with action="download_gdrive").

Common revisions:
- download_pdf failed "not a PDF" + content-type is text/html → maybe it's a Google
  Doc / Slides / Drive file → revise to download_gdrive.
- download_pdf failed with 404 → inspect to check the redirected URL; if it's now
  a different host, revise with the same action but the new URL is unknown — usually
  better to leave it (the original URL is dead).
- download_github_zip failed → inspect the URL; if it's a redirect to a different
  repo, you can revise; otherwise leave alone.
- Anything genuinely behind login (Content-Type=text/html with "Sign in" / "Login"
  hints) → do NOT revise; the original failure is correct. The pipeline already
  records paywalled items elsewhere.

When you have processed every failure, return a final JSON object:
  {"revised": [PlanItem, ...]}
listing only the items you want to retry. Items NOT in the list are left as-is.
No prose, no markdown fences.
"""

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "inspect_url",
            "description": "HEAD-peek a URL: status, content-type, length, final URL after redirects.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "revise",
            "description": "Propose a new PlanItem to retry for the given URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": [
                            "download_pdf",
                            "download_arxiv",
                            "download_github_zip",
                            "download_youtube",
                            "download_gdrive",
                            "note_paywalled",
                            "skip",
                        ],
                    },
                    "chapter": {"type": ["string", "null"]},
                    "subfolder": {
                        "type": ["string", "null"],
                        "enum": ["slides", "readings", "videos", "code", None],
                    },
                    "filename": {"type": ["string", "null"]},
                    "reason": {"type": "string"},
                },
                "required": ["url", "action", "reason"],
            },
        },
    },
]


_plan_adapter = TypeAdapter(list[PlanItem])


def _extract_revised(raw: str, collected_via_tool: list[PlanItem]) -> list[PlanItem]:
    """Prefer the agent's final {"revised": [...]} block; fall back to items collected via revise() calls."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return collected_via_tool
    items = data.get("revised") if isinstance(data, dict) else None
    if items is None:
        return collected_via_tool
    try:
        return _plan_adapter.validate_python(items)
    except ValidationError as exc:
        log.debug("Verifier final 'revised' failed validation: %s", exc)
        return collected_via_tool


def run_verifier(
    failures: list[tuple[PlanItem, DownloadResult]],
    runner: AgentRunner,
    log_callback: Callable[[str], None],
) -> list[PlanItem]:
    if not failures:
        return []

    collected: list[PlanItem] = []

    def dispatch(name: str, args: dict[str, Any]) -> Any:
        if name == "inspect_url":
            return tool_inspect_url(args.get("url", ""))
        if name == "revise":
            try:
                item = PlanItem(
                    url=args["url"],
                    action=args["action"],  # type: ignore[arg-type]
                    chapter=args.get("chapter"),
                    subfolder=args.get("subfolder"),  # type: ignore[arg-type]
                    filename=args.get("filename"),
                    reason=args.get("reason", ""),
                )
            except ValidationError as exc:
                return f"ERROR: invalid revised item: {exc}"
            collected.append(item)
            log_callback(f"  Verifier revised {item.url} → {item.action}")
            return {"accepted": True, "total_revised_so_far": len(collected)}
        return f"ERROR: unknown tool {name!r}"

    failure_payload = [
        {
            "url": item.url,
            "original_action": item.action,
            "filename": item.filename,
            "error_message": result.message,
        }
        for item, result in failures
    ]
    user = (
        f"There are {len(failures)} failed downloads to review.\n\n"
        f"Failures (JSON):\n{json.dumps(failure_payload, ensure_ascii=False, indent=2)}\n\n"
        "For each one, decide whether to call inspect_url + revise, or leave it alone. "
        "Return your final summary as {\"revised\": [...PlanItem...]}."
    )

    result = runner.run(
        agent_name="Verifier",
        system=SYSTEM,
        user=user,
        tools=TOOLS,
        dispatch=dispatch,
        max_steps=MAX_STEPS_VERIFIER,
        log_callback=log_callback,
    )

    revised = _extract_revised(result.final_message, collected)
    log_callback(f"Verifier proposes {len(revised)} retr{'y' if len(revised) == 1 else 'ies'}")
    return revised
