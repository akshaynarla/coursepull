"""What the UI shows, built from the run's event stream.

Pure Python, no NiceGUI: the same events drive the CLI, the UI and replays, and
this reducer can be tested on the events of a scripted run.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

# The agents in the order a run passes through them: node -> (name, what it does).
AGENTS = {
    "orchestrator": ("Orchestrator", "sends Scouts to the course pages"),
    "scouts": ("Scouts", "explore the pages, report materials"),
    "tutor": ("Tutor", "builds the course map and plan"),
    "reviewer": ("Reviewer", "critiques the plan"),
    "engine": ("Download engine", "downloads every planned item"),
    "examiner": ("Examiner", "checks what arrived"),
    "finish": ("Finish", "writes the course index"),
}
_NODE_ROLE = {"orchestrator": "orchestrator", "scouts": "scout", "tutor": "tutor", "reviewer": "reviewer",
              "examiner": "examiner"}
PHASES = {"discovery": "Discovering", "recovery": "Recovering", "planning": "Planning", "review": "Reviewing",
          "download": "Downloading", "examination": "Examining", "finished": "Finished"}
_ROLE_NODE = {"orchestrator": "orchestrator", "scout": "scouts", "tutor": "tutor", "reviewer": "reviewer",
              "examiner": "examiner"}
_PHASE_NODE = {"discovery": "orchestrator", "recovery": "orchestrator", "planning": "tutor", "review": "reviewer",
               "download": "engine", "examination": "examiner", "finished": "finish"}
COURSE_WIDE = None  # module key of items that belong to no module


@dataclass
class Item:
    task_id: str
    name: str
    role: str
    module: str | None
    required: bool = True
    kind: str | None = None
    status: str = "planned"  # planned, queued, running, done, failed, cancelled
    bytes_done: int = 0
    bytes_total: int | None = None
    files: list[str] = field(default_factory=list)
    message: str = ""
    error: str | None = None
    children: dict[str, str] = field(default_factory=dict)  # Drive folder entries: task id -> status

    @property
    def fraction(self) -> float | None:
        if self.status == "done":
            return 1.0
        if self.children:
            finished = sum(1 for s in self.children.values() if s in ("done", "failed", "cancelled"))
            return finished / len(self.children)
        if self.bytes_total:
            return min(self.bytes_done / self.bytes_total, 1.0)
        return None


@dataclass
class Line:
    ts: float
    agent: str
    text: str
    kind: str  # narration, tool, page, phase, info, error


@dataclass
class Scout:
    found: int = 0
    pages: int = 0
    done: bool = False
    stopped: str = ""


@dataclass
class RunView:
    status: str = "idle"  # idle, running, stopping, finished, error
    course_url: str = ""
    output_dir: str = ""
    resuming: bool = False
    models: dict[str, str] = field(default_factory=dict)
    phase: str = ""
    active: str = ""
    visited: list[str] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)
    scouts: dict[str, Scout] = field(default_factory=dict)
    title: str = ""
    modules: list[tuple[str, str]] = field(default_factory=list)
    items: dict[str, Item] = field(default_factory=dict)
    excluded: int = 0
    review: dict | None = None
    examination: dict | None = None
    downloads_total: int = 0
    downloads_done: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    tokens_used: int = 0
    token_limit: int = 0
    by_role: dict[str, int] = field(default_factory=dict)
    summary: dict | None = None
    error: str = ""
    # Bumped whenever a part of the view changes, so the UI only redraws what changed.
    versions: dict[str, int] = field(default_factory=lambda: {"map": 0, "agents": 0, "status": 0})

    # ---------- derived ----------

    @property
    def failed(self) -> int:
        return sum(1 for i in self.items.values() if i.status == "failed")

    @property
    def finished_items(self) -> int:
        """Planned items whose download has ended, across every download round of the run."""
        return sum(1 for i in self.items.values() if i.status in ("done", "failed", "cancelled"))

    @property
    def bytes_done(self) -> int:
        return sum(i.bytes_done for i in self.items.values())

    @property
    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    def module_groups(self) -> list[tuple[str, list[Item]]]:
        """(heading, items) per module in teaching order, then course-wide items."""
        keys = {key for key, _ in self.modules}
        groups = [(f"{key} · {title}", [i for i in self.items.values() if i.module == key])
                  for key, title in self.modules]
        groups.append(("Course-wide", [i for i in self.items.values() if i.module not in keys]))
        return [(heading, items) for heading, items in groups if items]

    def agent_state(self, node: str) -> str:
        """active, done or idle: how the agent list shows this agent."""
        if node == self.active and self.status in ("running", "stopping"):
            return "active"
        return "done" if node in self.visited else "idle"

    def agent_caption(self, node: str) -> str:
        """One line of what the agent has done so far; what it does before it starts."""
        caption = ""
        if node == "orchestrator" and self.scouts:
            caption = f"{len(self.scouts)} Scout{'s' if len(self.scouts) != 1 else ''} sent"
        elif node == "scouts" and self.scouts:
            found = sum(s.found for s in self.scouts.values())
            pages = sum(s.pages for s in self.scouts.values())
            working = sum(1 for s in self.scouts.values() if not s.done)
            caption = f"{found} found · {pages} page{'s' if pages != 1 else ''}" + (
                f" · {working} working" if working else "")
        elif node == "tutor" and self.items:
            caption = f"{len(self.items)} items in {len(self.modules)} modules"
        elif node == "reviewer" and self.review:
            caption = "approved the plan" if self.review["approved"] else "asked for changes"
        elif node == "engine" and self.finished_items:
            caption = f"{self.finished_items}/{len(self.items)} items" + (
                f" · {self.failed} failed" if self.failed else "")
        elif node == "examiner" and self.examination:
            caption = "passed the result" if self.examination["passed"] else "did not pass the result"
        elif node == "finish" and self.summary:
            caption = "course index written"
        if not caption:
            return AGENTS[node][1]
        tokens = self.by_role.get(_NODE_ROLE.get(node, ""), 0)
        return caption + (f" · {tokens / 1000:,.0f}k tokens" if tokens else "")

    # ---------- reducer ----------

    def _bump(self, *parts: str) -> None:
        for part in parts:
            self.versions[part] += 1

    def _line(self, event: dict, text: str, kind: str, agent: str | None = None) -> None:
        self.lines.append(Line(event.get("ts", time.time()), agent or event.get("agent", ""), text, kind))

    def _activate(self, node: str) -> None:
        if node and node != self.active:
            self.active = node
            if node not in self.visited:
                self.visited.append(node)
            self._bump("agents")

    def apply(self, event: dict) -> None:
        handler = getattr(self, f"_on_{event.get('type')}", None)
        if handler is not None:
            handler(event)

    def _on_run(self, e: dict) -> None:
        self.status, self.course_url, self.output_dir = "running", e["course_url"], e["output_dir"]
        self.resuming, self.models, self.token_limit = e["resuming"], e.get("models", {}), e.get("budget", 0)
        self.started_at, self.finished_at = e.get("ts", time.time()), None
        self._line(e, ("Resuming " if self.resuming else "Starting ") + self.course_url, "info", "CoursePull")
        self._bump("status", "agents")

    def _on_phase(self, e: dict) -> None:
        if e["phase"] != self.phase:
            self.phase = e["phase"]
            extra = f" ({e['items']} items)" if "items" in e else ""
            self._line(e, f"{e['phase']}{extra}", "phase", "")
            self._bump("status")
        self._activate(_PHASE_NODE.get(e["phase"], self.active))

    def _on_narration(self, e: dict) -> None:
        self._line(e, e["text"], "narration")
        self._activate(_ROLE_NODE.get(e.get("role", ""), self.active))

    def _on_tool(self, e: dict) -> None:
        self._line(e, f"→ {e['tool']}" + ("" if e.get("ok", True) else " (failed, retrying)"), "tool")
        self._activate(_ROLE_NODE.get(e.get("role", ""), self.active))

    def _on_page(self, e: dict) -> None:
        self._line(e, f"read {e['url']} ({e['links']} links" + (", tabs revealed)" if e.get("revealed") else ")"),
                   "page")
        if e["agent"].startswith("Scout"):
            self.scouts.setdefault(e["agent"], Scout()).pages += 1
            self._bump("agents")

    def _on_found(self, e: dict) -> None:
        self.scouts.setdefault(e["agent"], Scout()).found = e["total"]
        self._bump("agents")

    def _on_scout_done(self, e: dict) -> None:
        scout = self.scouts.setdefault(e["agent"], Scout())
        scout.found, scout.pages, scout.done, scout.stopped = e["found"], e["pages"], True, e.get("stopped", "")
        self._line(e, f"done: {e['found']} materials from {e['pages']} pages"
                   + (f" (stopped: {e['stopped']})" if e.get("stopped") else ""), "info")
        self._bump("agents")

    def _on_plan(self, e: dict) -> None:
        self.title = e.get("title") or self.title
        self.modules = [(m["key"], m["title"]) for m in e.get("modules", [])]
        self.excluded = e.get("excluded", 0)
        old = self.items
        self.items = {}
        for entry in e.get("entries", []):
            item = old.get(entry["task_id"]) or Item(task_id=entry["task_id"], name=entry["name"],
                                                    role=entry["role"], module=entry["module"])
            item.name, item.role, item.module = entry["name"], entry["role"], entry["module"]
            item.required, item.kind = entry.get("required", True), entry.get("kind")
            self.items[entry["task_id"]] = item
        self._line(e, f"plan: {e['planned']} items in {len(self.modules)} modules, {self.excluded} left out", "info")
        self._bump("map", "status")

    def _on_review(self, e: dict) -> None:
        self.review = e
        self._line(e, ("approved: " if e["approved"] else "changes requested: ") + e["summary"], "narration")
        self._activate("reviewer")
        self._bump("status")

    def _on_download(self, e: dict) -> None:
        self.downloads_total, self.downloads_done = e.get("total", 0), e.get("done", 0)
        item, parent = self.items.get(e["task_id"]), self.items.get(e.get("parent_id") or "")
        if item is None and parent is not None:  # an entry of a Drive folder
            parent.children[e["task_id"]] = e.get("status") or {"queued": "queued", "started": "running"}.get(
                e["event"], parent.children.get(e["task_id"], "queued"))
            self._bump("map", "status")
            return
        if item is None:
            return
        if e["event"] == "queued":
            item.status = "queued"
        elif e["event"] == "started":
            item.status = "running"
        elif e["event"] == "progress":
            item.bytes_done, item.bytes_total = e.get("bytes_done", 0), e.get("bytes_total")
        elif e["event"] == "finished":
            item.status = e.get("status", "done")
            item.files, item.message, item.error = e.get("files", []), e.get("message", ""), e.get("error")
            item.bytes_done = e.get("bytes_done", item.bytes_done)
            if item.status == "failed":
                self._line(e, f"✗ {item.name}: {item.error}: {item.message}", "error", "Download engine")
        self._activate("engine")
        self._bump("map", "status")

    def _on_examination(self, e: dict) -> None:
        self.examination = e
        self._line(e, ("passed: " if e["passed"] else "not passed: ") + e["summary"], "narration")
        self._activate("examiner")
        self._bump("status")

    def _on_tokens(self, e: dict) -> None:
        self.tokens_used, self.token_limit = e["used"], e.get("limit", self.token_limit)
        self._bump("status")

    def _on_usage(self, e: dict) -> None:
        self.by_role, self.tokens_used = e.get("by_role", {}), e.get("total", self.tokens_used)
        self._bump("status")

    def _on_done(self, e: dict) -> None:
        self.summary = e
        self.finished_at = e.get("ts", time.time())
        self._activate("finish")
        self._line(e, f"done: {e['files']} files from {e['planned']} planned items, {e['failed']} failed", "info",
                   "CoursePull")
        self.status = "finished"
        self._bump("status", "map")

    def _on_error(self, e: dict) -> None:
        self.status, self.error = "error", e["message"]
        self.finished_at = e.get("ts", time.time())
        self._line(e, e["message"], "error", "CoursePull")
        self._bump("status", "agents")


def short_name(name: str, module: str | None) -> str:
    """An item's name without the module prefix its card already shows ("Week 02 - Reading - X" -> "Reading - X")."""
    if not module or not module.isdigit():
        return name
    prefix = rf"^(?:week|lecture|lec|module|unit|session|w)\s*0*{int(module)}\s*[-–—:·|]\s*"
    stripped = re.sub(prefix, "", name, flags=re.IGNORECASE)
    return stripped or name
