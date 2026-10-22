"""Orchestrator: reads the course site, splits the work among Scouts, decides when
discovery is done, and sends Scouts back out to recover what the Examiner flags.
"""

from __future__ import annotations

import string
from collections import Counter
from typing import Literal

from pydantic import BaseModel, Field

from ..config import MAX_SCOUT_WAVES, MAX_SCOUTS_PER_WAVE
from ..identify import identify
from ..run_context import RunContext, emit
from ..state import CourseState, Material, ScoutAssignment, ScoutReport, SiteBrief
from .loop import Tool, ToolError, run_agent
from .terms import check_term_choice
from .toolbox import LinkBook, web_tools

SYSTEM = f"""\
You are the Orchestrator of CoursePull, a team of agents that collects the study materials of \
an open online course so a student can study it offline. You lead the team: you work out how \
the course website is organised and send Scouts to explore it in parallel. A Tutor then decides \
what to download, a Reviewer checks that plan, and an Examiner checks the downloads.

Only public material is collected; anything behind a login is noted, never worked around.

Scouts: at most {MAX_SCOUTS_PER_WAVE} per wave. Give each a distinct area of the course site \
(e.g. "schedule and lecture materials", "readings", "assignments and projects"), the start URLs \
for that area, and a brief saying what to look for. If everything sits on one page, one Scout \
is enough. The tabs of one page belong to one Scout (reveal_page clicks all of them): never give \
a tab its own Scout. Start URLs must be pages of the course site itself, not files. Every Scout \
costs time and money: send as few as cover the site.
"""

DISCOVER = """\
Read the course homepage with fetch_page. If it lists controls that may hide content (tabs such \
as "Syllabus" or "Schedule"), use reveal_page to see what they hide. Look at enough of the site \
to understand its structure, then call dispatch.

Several offerings: some sites host more than one offering of the course, e.g. the current term \
(possibly still in progress) and an archived earlier term on its own page. Page listings tell \
you when a page links other offerings ("other offerings of this course linked here: Fall 2025 \
-> ..."); don't guess URLs for terms. The student wants only the MOST COMPLETE offering: reveal \
each offering's schedule, pick the one whose lectures have the most materials, set term (e.g. \
"Fall 2025") and term_reason, and send Scouts only to that offering's pages. dispatch checks your \
choice against what the pages say. Leave term empty when there is only one offering."""

DECIDE = """\
The Scouts have reported (below). Decide whether another wave of scouting is needed: only if \
there are unexplored pages of the course site that likely hold course materials (lecture or \
week pages, a readings list, assignments). Don't scout external sites, people's pages or blogs. \
Call decide with action "scout" and new assignments, or action "plan" to hand over to the Tutor."""

RECOVER = """\
The Examiner checked the downloads and asks for recovery (below). Send Scouts to find \
replacements: an alternative link on the course site, a PDF version of a page, an arXiv or \
other open-access copy of a paper. Skip items that need a login or are paywalled with no open \
copy. Call recover with assignments, or with none if nothing is worth retrying."""


class AssignmentArgs(BaseModel):
    area: str
    start_urls: list[str] = Field(min_length=1)
    brief: str


class DispatchArgs(BaseModel):
    course_title: str
    summary: str = Field(description="One or two sentences on what the course is about")
    structure: str = Field(description="How the site organises its materials")
    term: str = Field("", description="Only if the site hosts several offerings: the one to collect")
    term_reason: str = Field("", description="Why that offering, e.g. 'Fall 2026 is in progress (4 of 10 weeks)'")
    assignments: list[AssignmentArgs] = Field(min_length=1)


class DecideArgs(BaseModel):
    action: Literal["scout", "plan"]
    reason: str
    assignments: list[AssignmentArgs] = Field(default_factory=list)


class RecoverArgs(BaseModel):
    reason: str
    assignments: list[AssignmentArgs] = Field(default_factory=list)


def _assignments(items: list[AssignmentArgs], wave: int, recovery_for: list[str] | None = None,
                 visited: dict[str, str] | None = None) -> list[ScoutAssignment]:
    """Validate and number a wave. visited (normalised url -> scout id) refuses re-scouting pages."""
    if len(items) > MAX_SCOUTS_PER_WAVE:
        raise ToolError(f"at most {MAX_SCOUTS_PER_WAVE} scouts per wave; merge some areas")
    for item in items:
        bad = [u for u in item.start_urls if not u.startswith(("http://", "https://"))]
        if bad:
            raise ToolError(f"start_urls must be absolute http(s) URLs: {bad}")
        if visited and (seen := {u: f"Scout {visited[_norm(u)]}" for u in item.start_urls if _norm(u) in visited}):
            raise ToolError(f"already read, with findings recorded: {seen}. Send Scouts only to unexplored "
                            "pages, or choose action 'plan'.")
    owners: dict[str, str] = {}
    for item in items:
        for url in item.start_urls:
            if (other := owners.setdefault(_norm(url), item.area)) != item.area:
                raise ToolError(f"two Scouts would start on the same page {url} ({other!r} and {item.area!r}). "
                                "One Scout reads a page with all its tabs and records every kind of material on "
                                "it: merge these into one assignment.")
    return [
        ScoutAssignment(scout_id=f"{string.ascii_uppercase[i]}{wave}", area=item.area,
                        start_urls=item.start_urls, brief=item.brief, recovery_for=recovery_for or [])
        for i, item in enumerate(items)
    ]


def _norm(url: str) -> str:
    return url.split("#")[0].rstrip("/").lower()


def cover_homepage(assignments: list[ScoutAssignment], homepage: str) -> list[ScoutAssignment]:
    """The homepage often links course-wide documents (syllabus, calendar); some Scout must read it."""
    if any(_norm(u) == _norm(homepage) for a in assignments for u in a.start_urls):
        return assignments
    first = assignments[0]
    widened = first.model_copy(update={
        "start_urls": [homepage, *first.start_urls],
        "brief": first.brief + " Also record course-wide documents linked from the homepage "
                                "(syllabus, calendar, policies).",
    })
    return [widened, *assignments[1:]]


def _announce(assignments: list[ScoutAssignment], why: str) -> None:
    areas = "; ".join(f"Scout {a.scout_id}: {a.area}" for a in assignments)
    emit({"type": "narration", "role": "orchestrator", "agent": "Orchestrator",
          "text": f"{why} Dispatching {len(assignments)} scout(s). {areas}"})


# ---------- the node ----------


def run_orchestrator(state: CourseState, rc: RunContext) -> dict:
    if not state.get("brief"):
        return _discover(state, rc)
    if state.get("awaiting_scouts"):
        return _after_wave(state, rc)
    if state.get("phase") == "recovery":
        return _recover(state, rc)
    return {"assignments": []}


def _discover(state: CourseState, rc: RunContext) -> dict:
    url = state["course_url"]

    def dispatch(args: DispatchArgs):
        assignments = _assignments(args.assignments, wave=1)
        # The term decision must agree with what the pages say (titles, term-switcher links).
        check_term_choice(args.term, [u for a in assignments for u in a.start_urls], url, rc.pages.offerings)
        return args, assignments

    tools = web_tools(rc, LinkBook(), "Orchestrator") + [
        Tool("dispatch", "Describe the course and send out the first wave of Scouts.", DispatchArgs,
             dispatch, final=True),
    ]
    result = run_agent(role="orchestrator", name="Orchestrator", system=SYSTEM + "\n" + DISCOVER,
                       user=f"Course homepage: {url}", tools=tools, rc=rc)
    if result.finished:
        args, assignments = result.final
        brief = SiteBrief(course_title=args.course_title, summary=args.summary, structure=args.structure,
                          term=args.term, term_reason=args.term_reason,
                          offerings=dict(rc.pages.offerings.terms))
        if not brief.term:
            # With several offerings the homepage usually shows another term than the one chosen.
            assignments = cover_homepage(assignments, url)
    else:
        # Fall back to one Scout on the homepage rather than giving up.
        brief = SiteBrief(course_title=url, summary="", structure="unknown")
        assignments = [ScoutAssignment(scout_id="A1", area="whole course site", start_urls=[url],
                                       brief="Find all course materials.")]
    _announce(assignments, f"Course: {brief.course_title}.")
    return {"brief": brief, "assignments": assignments, "wave": 1, "awaiting_scouts": True,
            "phase": "discovery", "usage": {"orchestrator": result.tokens}}


def consolidate(state: CourseState) -> dict[str, Material]:
    """Merge every Scout finding into the material list; same resource = one material."""
    materials = dict(state.get("materials") or {})
    by_key = {identify(m.url).key: mid for mid, m in materials.items()}
    for found in state.get("findings") or []:
        key = identify(found.url).key
        if key in by_key:
            existing = materials[by_key[key]]
            update = {}
            if found.evidence and found.evidence not in existing.evidence:
                update["evidence"] = (existing.evidence + " || " + found.evidence)[:500]
            if found.found_on and found.found_on not in existing.found_on:
                update["found_on"] = f"{existing.found_on} | {found.found_on}"  # e.g. listed in two terms
            if existing.required is None and found.required is not None:
                update["required"] = found.required
            if extra := [k for k in found.within if k not in existing.within]:
                update["within"] = [*existing.within, *extra]
            if update:
                materials[existing.material_id] = existing.model_copy(update=update)
            continue
        material_id = f"M{len(materials) + 1}"
        materials[material_id] = found.model_copy(update={"material_id": material_id})
        by_key[key] = material_id
    return materials


def covered_pages(reports: list[ScoutReport]) -> tuple[dict[str, str], list[str]]:
    """Pages done (normalised url -> scout id), and pages worth scouting again.

    A page is done only if its Scout finished and found something. Run 6's only Scout ran out
    of tokens with nothing recorded, and counting its page as done blocked any retry.
    """
    done = {_norm(u): r.scout_id for r in reports if not r.stopped_early and r.found for u in r.pages_visited}
    retry = sorted({u for r in reports if r.stopped_early or not r.found for u in r.pages_visited
                    if _norm(u) not in done})
    return done, retry


def _after_wave(state: CourseState, rc: RunContext) -> dict:
    materials = consolidate(state)
    update: dict = {"awaiting_scouts": False, "materials": materials, "assignments": []}
    if reason := rc.stop_reason():
        return {**update, "stop_reason": reason}
    wave = state.get("wave", 1)
    if state.get("phase") == "recovery":
        return {**update, "phase": "recovery"}
    if wave >= MAX_SCOUT_WAVES or rc.budget.soft_exceeded():
        return {**update, "phase": "planning"}

    reports = state.get("reports") or []
    visited, retry = covered_pages(reports)
    leads = sorted({lead for r in reports for lead in r.leads if _norm(lead) not in visited})
    roles = Counter(m.role for m in materials.values())
    brief = state["brief"]
    summary = "\n".join([
        *([f"Collecting only the {brief.term} offering ({brief.term_reason})."] if brief.term else []),
        f"Wave {wave} of at most {MAX_SCOUT_WAVES} is done. {len(materials)} materials so far: "
        + ", ".join(f"{n} {role}" for role, n in roles.most_common()),
        "", "Scout reports:",
        *[f"- Scout {r.scout_id} ({r.area}): {r.found} found on {len(r.pages_visited)} pages, "
          f"{r.tokens:,} tokens" + (f"; stopped early: {r.stopped_early}" if r.stopped_early else "")
          + (f"; notes: {r.notes}" if r.notes else "") for r in reports],
        "", "Unexplored leads:", *([f"- {lead}" for lead in leads[:30]] or ["- none"]),
        "", "Pages already read (with all their tabs):",
        *([f"- {u} (Scout {sid})" for u, sid in sorted(visited.items())[:40]] or ["- none"]),
        *(["", "Pages whose Scout stopped early or found nothing (may be scouted again):",
           *[f"- {u}" for u in retry[:20]]] if retry else []),
    ])
    tools = [t for t in web_tools(rc, LinkBook(), "Orchestrator") if t.name in ("fetch_page", "inspect_url")]
    tools.append(Tool("decide", "Send another wave of Scouts, or hand over to the Tutor.", DecideArgs,
                      lambda a: (a, _assignments(a.assignments, wave=wave + 1, visited=visited)
                                 if a.action == "scout" else []),
                      final=True))
    result = run_agent(role="orchestrator", name="Orchestrator", system=SYSTEM + "\n" + DECIDE,
                       user=summary, tools=tools, rc=rc)
    update["usage"] = {"orchestrator": result.tokens}
    if result.finished:
        args, assignments = result.final
        if assignments:
            _announce(assignments, args.reason)
            return {**update, "assignments": assignments, "wave": wave + 1, "awaiting_scouts": True}
        emit({"type": "narration", "role": "orchestrator", "agent": "Orchestrator",
              "text": f"Discovery complete: {args.reason}"})
    return {**update, "phase": "planning"}


def _recover(state: CourseState, rc: RunContext) -> dict:
    exam = state["examination"]
    outcomes = state.get("outcomes") or {}
    entries = {e.task.task_id: e for e in state["plan"].entries}
    failed = [
        f"- task {tid} ({entries[tid].task.name if tid in entries else '?'}): "
        f"{o.status} {o.error_kind.value if o.error_kind else ''} {o.message} -> {o.url}"
        for tid, o in outcomes.items() if o.status != "done"
    ]
    user = "\n".join([
        f"Course: {state['brief'].course_title} ({state['course_url']})",
        f"Examiner's summary: {exam.summary}",
        "Recovery requests:",
        *[f"- {r.problem} | suggestion: {r.suggestion}" + (f" | task {r.task_id}" if r.task_id else "")
          for r in exam.recovery],
        *([f"Gaps: {'; '.join(exam.gaps)}"] if exam.gaps else []),
        "Failed downloads:", *(failed[:40] or ["- none"]),
    ])
    wave = state.get("wave", 1) + 1
    task_ids = [r.task_id for r in exam.recovery if r.task_id]
    tools = [t for t in web_tools(rc, LinkBook(), "Orchestrator") if t.name in ("fetch_page", "inspect_url")]
    tools.append(Tool("recover", "Send Scouts to find replacements, or none.", RecoverArgs,
                      lambda a: (a, _assignments(a.assignments, wave=wave, recovery_for=task_ids)), final=True))
    result = run_agent(role="orchestrator", name="Orchestrator", system=SYSTEM + "\n" + RECOVER,
                       user=user, tools=tools, rc=rc)
    usage = {"orchestrator": result.tokens}
    if result.finished and result.final[1]:
        args, assignments = result.final
        _announce(assignments, f"Recovery: {args.reason}")
        return {"assignments": assignments, "wave": wave, "awaiting_scouts": True, "usage": usage}
    emit({"type": "narration", "role": "orchestrator", "agent": "Orchestrator",
          "text": "Nothing worth recovering; wrapping up."})
    return {"assignments": [], "phase": "finished", "usage": usage}
