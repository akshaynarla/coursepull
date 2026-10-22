"""The blackboard: typed shared state passed between the agents and the engine.

Agents never hand each other free text. Scouts report Materials, the Tutor turns
them into a CourseMap and a Plan of DownloadTasks, the Reviewer answers with a
Review, the engine with TaskOutcomes, and the Examiner with an Examination.
CourseState is the LangGraph state that holds all of it and is checkpointed.
"""

from __future__ import annotations

import hashlib
import operator
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field, model_validator


class ResourceKind(str, Enum):
    GOOGLE_DOC = "google_doc"
    GOOGLE_SLIDES = "google_slides"
    GOOGLE_SHEET = "google_sheet"
    DRIVE_FILE = "drive_file"
    DRIVE_FOLDER = "drive_folder"
    VIDEO = "video"
    PAPER = "paper"
    REPO = "repo"
    FILE = "file"
    WEBPAGE = "webpage"
    UNKNOWN = "unknown"


class ErrorKind(str, Enum):
    AUTH_REQUIRED = "auth_required"  # needs a login, or not shared publicly
    PAYWALLED = "paywalled"  # no open-access copy exists
    FORBIDDEN = "forbidden"  # 403, including owner-disabled downloads
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    NOT_A_FILE = "not_a_file"  # got an HTML page where a file was expected
    TOO_LARGE = "too_large"
    UNSUPPORTED = "unsupported"
    NETWORK = "network"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ResourceRef(BaseModel):
    """What a URL points at, as far as can be told from the URL alone."""

    url: str
    kind: ResourceKind
    provider: str = "generic"
    resource_id: str | None = None
    published: bool = False  # Google "publish to the web" link (no export endpoint)
    download_url: str | None = None
    login_likely: bool = False
    hint: str = ""
    params: dict[str, str] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        """Canonical identity: different URLs for the same resource share a key."""
        if self.resource_id:
            return f"{self.provider}:{self.resource_id}"
        return self.url


def make_task_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


class DownloadTask(BaseModel):
    """One item of the plan, as handed to the download engine."""

    task_id: str = ""
    url: str
    kind: ResourceKind | None = None  # None: let identify() decide
    folder: list[str] = Field(default_factory=list)  # path parts under the output dir
    name: str | None = None  # human title, becomes the filename stem
    formats: list[str] = Field(default_factory=list)  # Google exports; empty = defaults
    note: str = ""  # why the agent wants it; carried into the course index
    term: str = ""  # the offering being collected; repositories prefer the branch named after it
    parent_id: str | None = None
    depth: int = 0

    @model_validator(mode="after")
    def _default_id(self) -> DownloadTask:
        if not self.task_id:
            self.task_id = make_task_id(self.url)
        return self


TaskStatus = Literal["queued", "running", "done", "failed", "cancelled"]


class TaskOutcome(BaseModel):
    task_id: str
    url: str
    kind: ResourceKind
    status: TaskStatus
    files: list[str] = Field(default_factory=list)
    bytes: int = 0
    error_kind: ErrorKind | None = None
    message: str = ""
    children: list[str] = Field(default_factory=list)  # task ids spawned, e.g. Drive folder entries


EventType = Literal["queued", "started", "progress", "finished"]


@dataclass(frozen=True)
class EngineEvent:
    type: EventType
    task_id: str
    url: str = ""
    bytes_done: int = 0
    bytes_total: int | None = None
    outcome: TaskOutcome | None = None
    ts: float = field(default_factory=time.time)


# ---------- what the agents produce ----------

MaterialRole = Literal[
    "lecture_slides", "lecture_notes", "lecture_video", "reading", "assignment", "solution",
    "exam", "project", "code", "dataset", "syllabus", "other",
]


class SiteBrief(BaseModel):
    """The Orchestrator's first read of the course site."""

    course_title: str
    summary: str = ""
    structure: str = ""  # how the site organises material: weekly table, lecture pages, Drive folder...
    term: str = ""  # when the site hosts several offerings: the one being collected (the most complete)
    term_reason: str = ""
    offerings: dict[str, str] = Field(default_factory=dict)  # page url -> term, as established by code


class ScoutAssignment(BaseModel):
    scout_id: str
    area: str  # e.g. "Schedule and lecture slides"
    start_urls: list[str]
    brief: str  # what to look for and why
    recovery_for: list[str] = Field(default_factory=list)  # task ids this assignment tries to replace


class Material(BaseModel):
    """Something a Scout found that may be needed for the course."""

    material_id: str = ""  # "M12"; assigned when findings are consolidated
    url: str
    title: str
    kind: ResourceKind
    role: MaterialRole
    module_hint: str | None = None  # the module as the page names it, e.g. "Week 3"
    required: bool | None = None
    evidence: str = ""  # heading path and row text it was found under
    found_on: str = ""
    scout_id: str = ""
    note: str = ""
    within: list[str] = Field(default_factory=list)  # keys of the Drive folders it sits in, innermost first


class ScoutReport(BaseModel):
    scout_id: str
    area: str
    pages_visited: list[str] = Field(default_factory=list)
    found: int = 0
    leads: list[str] = Field(default_factory=list)  # pages worth scouting next
    notes: str = ""
    stopped_early: str = ""  # why the scout stopped before finishing, if it did
    tokens: int = 0
    page_titles: dict[str, str] = Field(default_factory=dict)  # page url -> title, for telling terms apart


class Module(BaseModel):
    key: str  # "03"
    title: str  # "Policy Gradients"


class CourseMap(BaseModel):
    title: str
    summary: str = ""
    modules: list[Module] = Field(default_factory=list)


class PlanEntry(BaseModel):
    material_id: str
    task: DownloadTask
    module_key: str | None = None
    role: MaterialRole
    required: bool = True
    rationale: str = ""


class Exclusion(BaseModel):
    material_id: str
    url: str
    reason: str


class Plan(BaseModel):
    entries: list[PlanEntry] = Field(default_factory=list)
    excluded: list[Exclusion] = Field(default_factory=list)
    summary: str = ""


class ReviewIssue(BaseModel):
    severity: Literal["blocker", "major", "minor"]
    category: Literal["missing", "noise", "mislabel", "misplaced", "duplicate", "other"]
    detail: str
    material_ids: list[str] = Field(default_factory=list)


class Review(BaseModel):
    approved: bool
    summary: str
    issues: list[ReviewIssue] = Field(default_factory=list)


class Verdict(BaseModel):
    task_id: str
    ok: bool
    reason: str


class RecoveryRequest(BaseModel):
    problem: str
    suggestion: str
    task_id: str | None = None  # the download it replaces; None for a gap in the course map


class Examination(BaseModel):
    passed: bool
    summary: str
    verdicts: list[Verdict] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    recovery: list[RecoveryRequest] = Field(default_factory=list)


# ---------- LangGraph state ----------


def merge_dicts(left: dict, right: dict) -> dict:
    return {**(left or {}), **(right or {})}


def add_counts(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    merged = dict(left or {})
    for key, value in (right or {}).items():
        merged[key] = merged.get(key, 0) + value
    return merged


Phase = Literal["discovery", "planning", "review", "download", "examination", "recovery", "finished"]


class CourseState(TypedDict, total=False):
    course_url: str
    plan_only: bool  # stop after the reviewed plan (dry runs, plan evals)
    phase: Phase
    brief: SiteBrief
    # Orchestrator -> Scouts
    wave: int
    assignments: list[ScoutAssignment]
    awaiting_scouts: bool
    # Scouts -> Orchestrator (fan-in through the reducers)
    reports: Annotated[list[ScoutReport], operator.add]
    findings: Annotated[list[Material], operator.add]
    # Consolidated by the Orchestrator, keyed by material_id
    materials: dict[str, Material]
    # Tutor <-> Reviewer
    course_map: CourseMap
    plan: Plan
    review: Review
    review_rounds: int
    # Engine
    outcomes: Annotated[dict[str, TaskOutcome], merge_dicts]
    # Examiner
    examination: Examination
    recovery_rounds: int
    # Control
    stop_reason: str
    usage: Annotated[dict[str, int], add_counts]  # tokens per agent role


def blackboard_types() -> list[type]:
    """Every type that can sit in a checkpoint (the deserialisation allowlist)."""
    return [
        ResourceKind, ErrorKind,
        ResourceRef, DownloadTask, TaskOutcome, SiteBrief, ScoutAssignment, Material, ScoutReport,
        Module, CourseMap, PlanEntry, Exclusion, Plan, ReviewIssue, Review, Verdict, RecoveryRequest,
        Examination,
    ]
