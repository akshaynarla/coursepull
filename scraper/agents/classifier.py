"""Classifier agent: turns discovered links into a download plan."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from pydantic import TypeAdapter, ValidationError

from ..config import MAX_STEPS_CLASSIFIER
from ..models import Link, PlanItem
from .runtime import AgentRunner
from .tools import tool_inspect_url

log = logging.getLogger(__name__)


SYSTEM = """\
You are the Classifier agent. You are given a list of links scraped from a university
course site, and you must produce a download plan.

TOOL (use only when needed; each call costs time and tokens):
- inspect_url(url): HEAD-peek the URL; returns status, content-type, length, final URL.
  Use this only when the URL alone is ambiguous (e.g. a bit.ly redirect, a generic
  /resources/123 path that could be HTML or PDF, a Google Drive link you're unsure about).

For EACH input link, produce one JSON object with these fields:
- url: copy the original URL verbatim. REQUIRED for every item.
- action: one of
    download_pdf           PDF on the course site or institutional domain
    download_arxiv         arxiv.org paper link
    download_github_zip    github.com repo or directory
    download_youtube       youtube.com or youtu.be
    download_gdrive        public Google Drive file, Doc, Slides, or Sheet
                           (Drive FOLDERS → note_paywalled, no public archive endpoint)
    note_paywalled         behind login (video.ethz.ch, coursera, canvas, moodle,
                           Drive folder) or non-arxiv academic paper behind a paywall
    skip                   navigation, social media, author bios, unrelated links
- chapter: snake_case slug like "week02_robot_control_mdps" if the link belongs to a
           specific chapter/week/module. If the page has no chapter structure, use null
           for every item.
- subfolder: one of "slides" | "readings" | "videos" | "code" | null
- filename: leaf filename with the correct extension.
            Use the URL's original filename when present in the path.
            YouTube: slugified anchor text + ".mp4"
            GitHub directory: directory name + ".zip"
            Google Docs/Slides: slugified anchor + ".pdf"
            Google Sheets: slugified anchor + ".xlsx"
- reason: one short sentence justifying the decision.

FINAL OUTPUT: a JSON object {"plan": [...]} — and ONLY that JSON object. No prose,
no markdown fences, nothing outside it.
"""

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "inspect_url",
            "description": (
                "HEAD-peek a URL to disambiguate when the URL alone isn't enough. "
                "Returns status, content-type, content-length, final URL after redirects."
            ),
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
]


_THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")
_plan_adapter = TypeAdapter(list[PlanItem])


def _extract_plan(raw: str) -> list[PlanItem] | None:
    text = _THINK_RE.sub("", raw).strip()
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()

    candidates = [text]
    m = _JSON_OBJ_RE.search(text)
    if m:
        candidates.append(m.group(0))

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        items = data.get("plan") if isinstance(data, dict) else data
        if items is None:
            continue
        try:
            return _plan_adapter.validate_python(items)
        except ValidationError as exc:
            log.debug("Plan validation failed: %s", exc)
    return None


def run_classifier(
    course_url: str,
    pages: dict[str, list[Link]],
    runner: AgentRunner,
    log_callback: Callable[[str], None],
) -> list[PlanItem]:
    if not pages:
        return []

    # Union of all links across all crawled pages, deduplicated by URL.
    all_links: dict[str, Link] = {}
    for page_links in pages.values():
        for link in page_links:
            all_links.setdefault(link.url, link)

    links_payload = [link.model_dump() for link in all_links.values()]
    log_callback(f"Classifier: {len(all_links)} unique links across {len(pages)} page(s)")

    def dispatch(name: str, args: dict[str, Any]) -> Any:
        if name == "inspect_url":
            return tool_inspect_url(args.get("url", ""))
        return f"ERROR: unknown tool {name!r}"

    user = (
        f"Course URL: {course_url}\n\n"
        f"Pages crawled: {list(pages.keys())}\n\n"
        f"Links to classify (JSON, {len(links_payload)} items):\n"
        f"{json.dumps(links_payload, ensure_ascii=False)}"
    )
    result = runner.run(
        agent_name="Classifier",
        system=SYSTEM,
        user=user,
        tools=TOOLS,
        dispatch=dispatch,
        max_steps=MAX_STEPS_CLASSIFIER,
        log_callback=log_callback,
    )

    plan = _extract_plan(result.final_message)
    if plan is None:
        log_callback("Classifier returned an unparseable plan; see log file for raw response")
        log.debug("Classifier raw final message:\n%s", result.final_message)
        return []
    log_callback(f"Classifier produced a plan with {len(plan)} items")
    return plan
