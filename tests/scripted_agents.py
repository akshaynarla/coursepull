"""Scripted stand-ins for the LLM, one "brain" per agent role.

They drive the real graph, tools and engine through every path deterministically,
by reading the same tool results a model would.
"""

from __future__ import annotations

import itertools
import json
import re
import threading

from scraper.llm import LLMRouter, ToolCall, Turn

_ids = itertools.count(1)
LINK = re.compile(r'^(L\d+) \[([^\]]+)\] "([^"]*)"(.*?)-> (\S+)$')
MATERIAL = re.compile(r"^(M\d+) \| (\w+) \| \w+ \| '([^']*)' \| module: ([^|]+) \|")


def call(name: str, **args) -> Turn:
    return Turn(content=f"Calling {name}.", tool_calls=[ToolCall(f"c{next(_ids)}", name, json.dumps(args))],
                tokens=100)


def tool_results(messages: list[dict]) -> list[str]:
    return [m["content"] for m in messages if m["role"] == "tool"]


class Brains:
    """Holds the per-role scripts and counters shared across threads."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.lock = threading.Lock()
        self.calls: dict[str, int] = {}
        self.fail_tutor_once = False
        self.reviews = 0
        self.scouted: list[str] = []  # areas, in the order scouts started

    def count(self, role: str) -> None:
        with self.lock:
            self.calls[role] = self.calls.get(role, 0) + 1

    def router(self) -> LLMRouter:
        return LLMRouter({role: _Client(self, role) for role in
                          ("orchestrator", "scout", "tutor", "reviewer", "examiner")})

    # ---------- per-role scripts ----------

    def orchestrator(self, messages: list[dict]) -> Turn:
        system, done = messages[0]["content"], tool_results(messages)
        if "Read the course homepage" in system:
            if not done:
                url = messages[1]["content"].split(": ", 1)[1]
                return call("fetch_page", url=url)
            return call("dispatch", course_title="Testing 101", summary="How to test software.",
                        structure="A schedule table and a readings page.", assignments=[
                            {"area": "schedule", "start_urls": [f"{self.base}/course/"], "brief": "lecture slides"},
                            {"area": "readings", "start_urls": [f"{self.base}/course/readings.html"],
                             "brief": "papers"},
                        ])
        if "Scouts have reported" in system:
            return call("decide", action="plan", reason="all areas covered")
        return call("recover", reason="Paper B has a mirror", assignments=[
            {"area": "Paper B mirror", "start_urls": [f"{self.base}/course/readings.html"],
             "brief": "find the mirror of Paper B"}])

    def scout(self, messages: list[dict]) -> Turn:
        user, done = messages[1]["content"], tool_results(messages)
        if not done:
            with self.lock:
                self.scouted.append(re.search(r"Your area: (.+)", user).group(1))
            start = re.search(r"Start pages:\n- (\S+)", user).group(1)
            return call("fetch_page", url=start)
        if done[-1].startswith("PAGE"):
            want_mirror = "mirror" in user
            items = []
            for line in done[-1].splitlines():
                m = LINK.match(line)
                if not m or not m.group(5).endswith(".pdf"):
                    continue
                link_id, _tag, text, rest, url = m.groups()
                if ("mirror" in url) != want_mirror:
                    continue
                week = re.search(r"Week \d", rest)
                lecture = re.fullmatch(r"l\d+\.pdf", url.rsplit("/", 1)[-1])
                items.append({"link": link_id, "title": text, "module": week.group(0) if week else None,
                              "role": "lecture_slides" if lecture else "reading", "required": True})
            return call("record_materials", items=items)
        return call("finish", leads=[], notes="")

    def tutor(self, messages: list[dict]) -> Turn:
        with self.lock:
            if self.fail_tutor_once:
                self.fail_tutor_once = False
                raise RuntimeError("LLM endpoint is down")
        system, user, done = messages[0]["content"], messages[1]["content"], tool_results(messages)
        if "Reviewer checked" in system:
            return call("finish", summary="names fixed")
        recovering = "Downloads were checked" in system
        if not done and not recovering:
            return call("set_course_map", title="Testing 101", summary="How to test software.",
                        modules=[{"key": "01", "title": "Intro"}, {"key": "02", "title": "Mocks"}])
        if len(done) <= 1:
            section = user.split("New materials:")[-1] if recovering else user
            items = []
            for line in section.splitlines():
                if m := MATERIAL.match(line):
                    mid, _role, title, module = m.groups()
                    key = {"Week 1": "01", "Week 2": "02"}.get(module.strip())
                    items.append({"material": mid, "module": key, "name": title, "required": True,
                                  "rationale": "needed"})
            unplan = [call("unplan", materials=[re.search(r"(M\d+) ->.*'Paper B'", user).group(1)],
                           reason="replaced by the mirror")] if recovering else []
            return unplan[0] if recovering and not done else call("plan", items=items)
        return call("finish", summary="everything planned")

    def reviewer(self, messages: list[dict]) -> Turn:
        with self.lock:
            self.reviews += 1
            first = self.reviews == 1
        if first:
            return call("submit_review", approved=False, summary="Lecture names are unclear",
                        issues=[{"severity": "major", "category": "mislabel", "detail": "rename", "material_ids": []}])
        return call("submit_review", approved=True, summary="Looks complete")

    def examiner(self, messages: list[dict]) -> Turn:
        failed = [line.split(" | ")[0] for line in messages[1]["content"].splitlines() if "FAILED" in line]
        if failed:
            return call("submit_examination", passed=False, summary="Paper B is missing",
                        recovery=[{"problem": "Paper B link is dead", "suggestion": "use the mirror",
                                   "task_id": failed[0]}])
        return call("submit_examination", passed=True, summary="All materials present")


class _Client:
    def __init__(self, brains: Brains, role: str) -> None:
        self.brains, self.role, self.model = brains, role, f"scripted-{role}"

    def complete(self, messages: list[dict], tools: list[dict]) -> Turn:
        self.brains.count(self.role)
        return getattr(self.brains, self.role)(messages)
