"""Tutor: decides what a student needs from everything the Scouts found, and
organises it into a course map and a download plan.

The Tutor only plans; paths are composed by code from its choices.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..engine.paths import safe_component
from ..identify import identify
from ..run_context import RunContext, emit
from ..state import (
    CourseMap, CourseState, DownloadTask, Exclusion, Material, MaterialRole, Module, Plan, PlanEntry, ResourceKind,
)
from .checks import doubts
from .loop import Tool, ToolError, run_agent

ROLE_FOLDERS: dict[str, str] = {
    "lecture_slides": "slides", "lecture_notes": "notes", "lecture_video": "videos", "reading": "readings",
    "assignment": "assignments", "solution": "solutions", "exam": "exams", "project": "project",
    "code": "code", "dataset": "data", "syllabus": "", "other": "other",
}
COURSE_WIDE = "Course"

SYSTEM = """\
You are the Tutor in CoursePull, a team of agents that collects the study materials of an open \
online course so a student can study it offline. Scouts have explored the course website and \
found the materials listed below. You decide what the student needs and organise it.

1. set_course_map: the course title, a one-sentence summary, and its modules (weeks, lectures \
or units) in teaching order, keyed "01", "02", ... Use the course's own structure.
2. plan every material the student needs: its module key (null for course-wide items such as \
the syllabus), a clear file name without extension (e.g. "Lecture 03 - Policy Gradients"), \
whether it is required, and a one-line rationale. Fix the role if a Scout got it wrong.
3. exclude everything else, with a reason: not course material, a duplicate of a planned item, \
only reachable with a login, an unrelated external page.

Guidelines:
- A student needs the lectures (slides, notes, recordings), readings, assignments and their \
materials, solutions and exams if published, code and data used in the course.
- Don't take the Scouts' labels on trust. Each material shows what kind of link it really is, \
and "doubts:" where something looks off. A speaker's homepage or LinkedIn profile is not a \
lecture recording; exclude it. Social media pages are excluded unless the course explicitly \
assigns that post as a reading.
- Keep one copy of each resource. If a lecture has the same deck as Google Slides and as a PDF, \
plan one (Google Slides gives PDF and PPTX) and exclude the other as a duplicate.
- How things are saved: a code repository downloads as a zip of all its code. When a repository \
is listed as a reading rather than as course code, plan it with kind="webpage" to save its \
README page instead.
- required is your call for every item: true for lecture material, assignments and readings the \
course marks as required; false for optional, further or background reading and extra videos.
- One offering only: if the course brief names a term, plan only that term's materials and \
exclude anything from another offering (reason "other offering: <term>"). Each material shows \
the page it was found on ("on:"), and the pages' titles tell you which offering they belong to. \
Course-wide documents of another term (e.g. a "FA26 Calendar" when collecting Fall 2025) are \
excluded too.
- Every material must end up planned or excluded; finish refuses otherwise.
Call finish with a short summary when done.
"""

REVISE = """\
The Reviewer checked your plan and raised the issues below. Fix them with plan, unplan and \
exclude (reading the current plan below), then call finish again."""

RECOVER = """\
Downloads were checked and Scouts went looking for replacements. Plan the new materials that \
replace failed or wrong downloads (unplan the ones they replace), exclude the rest, then finish."""


class ModuleArgs(BaseModel):
    key: str = Field(description='"01", "02", ...')
    title: str


class CourseMapArgs(BaseModel):
    title: str
    summary: str
    modules: list[ModuleArgs]


class PlanItemArgs(BaseModel):
    material: str = Field(description="Material id, e.g. M12")
    module: str | None = Field(None, description="Module key, or null for course-wide items")
    name: str = Field(description="File name without extension")
    required: bool = Field(description="true = core/required material, false = optional or further reading")
    rationale: str
    role: MaterialRole | None = Field(None, description="Only to correct the Scout's role")
    kind: ResourceKind | None = Field(
        None, description="Only to change how it is saved, e.g. 'webpage' for a repository's README page")
    formats: list[str] = Field(default_factory=list, description="Google exports only, e.g. ['pdf','pptx']")


class PlanArgs(BaseModel):
    items: list[PlanItemArgs]


class ExcludeItem(BaseModel):
    material: str
    reason: str


class ExcludeArgs(BaseModel):
    items: list[ExcludeItem]


class UnplanArgs(BaseModel):
    materials: list[str]
    reason: str


class FinishArgs(BaseModel):
    summary: str


def folder_for(course_map: CourseMap | None, module_key: str | None, role: str) -> list[str]:
    module = next((m for m in (course_map.modules if course_map else []) if m.key == module_key), None)
    top = f"{module.key} - {module.title}" if module else COURSE_WIDE
    sub = ROLE_FOLDERS.get(role, "other")
    return [safe_component(top, fallback=COURSE_WIDE)] + ([sub] if sub else [])


def _kind_label(url: str, kind: ResourceKind) -> str:
    provider = identify(url).provider
    return kind.value + (f"/{provider}" if provider not in ("generic", "direct") else "")


def folder_index(materials: dict[str, Material]) -> dict[str, str]:
    """Drive folders among the materials: resource key -> material id."""
    return {identify(m.url).key: mid for mid, m in materials.items() if m.kind == ResourceKind.DRIVE_FOLDER}


def describe_material(m: Material, folders: dict[str, str] | None = None) -> str:
    required = {True: "required", False: "optional", None: "?"}[m.required]
    parts = [f"{m.material_id} | {m.role} | {_kind_label(m.url, m.kind)} | {m.title!r} | "
             f"module: {m.module_hint or '-'} | {required}"]
    if concerns := doubts(m.url, m.role, m.kind):
        parts.append("doubts: " + "; ".join(concerns))
    if m.evidence:
        parts.append(f"context: {m.evidence[:160]}")
    if m.note:
        parts.append(f"note: {m.note[:100]}")
    if folders and (inside := [folders[k] for k in m.within if k in folders]):
        parts.append(f"inside Drive folder {', '.join(inside)} (planning that folder already downloads this file)")
    if m.found_on:
        parts.append(f"on: {m.found_on[:160]}")
    parts.append(m.url[:120])
    return " | ".join(parts)


def describe_pages(state: CourseState) -> list[str]:
    """The pages the Scouts read, with titles: how agents tell offerings (terms) apart."""
    pages = {url: title for r in state.get("reports") or [] for url, title in r.page_titles.items()}
    offerings = state["brief"].offerings if state.get("brief") else {}
    lines = []
    for url, title in sorted(pages.items()):
        term = offerings.get(url.split("#")[0].rstrip("/").lower())
        lines.append(f"- {url}: {title or '(no title)'}" + (f" [the {term} offering]" if term else ""))
    return lines


def describe_term(state: CourseState) -> list[str]:
    brief = state["brief"]
    if not brief.term:
        return []
    return [f"Term to collect: {brief.term} (the most complete offering: {brief.term_reason})", ""]


def describe_entry(e: PlanEntry) -> str:
    kind = _kind_label(e.task.url, e.task.kind) if e.task.kind else "?"
    return (f"{e.material_id} -> module {e.module_key or '-'} / {e.role} / {kind} / {e.task.name!r}"
            f" ({'required' if e.required else 'optional'}): {e.rationale[:100]}")


def run_tutor(state: CourseState, rc: RunContext) -> dict:
    materials: dict[str, Material] = state.get("materials") or {}
    old_plan: Plan = state.get("plan") or Plan()
    course_map: list[CourseMap | None] = [state.get("course_map")]
    entries: dict[str, PlanEntry] = {e.material_id: e for e in old_plan.entries}
    excluded: dict[str, Exclusion] = {x.material_id: x for x in old_plan.excluded}

    def material(mid: str) -> Material:
        key = mid.strip().upper()
        if key not in materials:
            raise ToolError(f"unknown material id {mid!r}")
        return materials[key]

    def set_map(args: CourseMapArgs) -> str:
        course_map[0] = CourseMap(title=args.title, summary=args.summary,
                                  modules=[Module(key=m.key, title=m.title) for m in args.modules])
        return f"course map set with {len(args.modules)} modules"

    def plan(args: PlanArgs) -> str:
        if course_map[0] is None:
            raise ToolError("call set_course_map first")
        keys = {m.key for m in course_map[0].modules}
        problems = []
        for item in args.items:
            try:
                m = material(item.material)
            except ToolError as exc:
                problems.append(str(exc))
                continue
            if item.module is not None and item.module not in keys:
                problems.append(f"{item.material}: unknown module {item.module!r}")
                continue
            role = item.role or m.role
            entries[m.material_id] = PlanEntry(
                material_id=m.material_id, module_key=item.module, role=role, required=item.required,
                rationale=item.rationale,
                task=DownloadTask(url=m.url, kind=item.kind or m.kind,
                                  folder=folder_for(course_map[0], item.module, role),
                                  name=item.name, formats=item.formats, note=item.rationale),
            )
            excluded.pop(m.material_id, None)
        return f"{len(entries)} planned, {len(excluded)} excluded" + (
            "; problems: " + "; ".join(problems) if problems else "")

    def exclude(args: ExcludeArgs) -> str:
        for item in args.items:
            m = material(item.material)
            excluded[m.material_id] = Exclusion(material_id=m.material_id, url=m.url, reason=item.reason)
            entries.pop(m.material_id, None)
        return f"{len(entries)} planned, {len(excluded)} excluded"

    def unplan(args: UnplanArgs) -> str:
        for mid in args.materials:
            m = material(mid)
            entries.pop(m.material_id, None)
            excluded[m.material_id] = Exclusion(material_id=m.material_id, url=m.url, reason=args.reason)
        return f"{len(entries)} planned, {len(excluded)} excluded"

    def finish(args: FinishArgs) -> str:
        if course_map[0] is None:
            raise ToolError("call set_course_map first")
        open_ids = [mid for mid in materials if mid not in entries and mid not in excluded]
        if open_ids:
            raise ToolError(f"{len(open_ids)} materials are neither planned nor excluded: {', '.join(open_ids[:40])}")
        return args.summary

    tools = [
        Tool("set_course_map", "Set the course title, summary and modules.", CourseMapArgs, set_map),
        Tool("plan", "Plan materials for download (adds or updates).", PlanArgs, plan),
        Tool("exclude", "Exclude materials, with a reason.", ExcludeArgs, exclude),
        Tool("unplan", "Remove planned materials (they become excluded).", UnplanArgs, unplan),
        Tool("finish", "Finish planning.", FinishArgs, finish, final=True),
    ]

    phase = state.get("phase")
    brief = state["brief"]
    folders = folder_index(materials)
    sections = [f"Course: {brief.course_title} ({state['course_url']})", f"About: {brief.summary}",
                f"Site structure: {brief.structure}", "", *describe_term(state),
                "Pages the Scouts read:", *(describe_pages(state) or ["- none"]), ""]
    notes = [f"- Scout {r.scout_id}: {r.notes}" for r in state.get("reports") or [] if r.notes]
    if notes:
        sections += ["Scout notes:", *notes, ""]
    system = SYSTEM
    if phase == "recovery":
        system += "\n" + RECOVER
        exam = state["examination"]
        new = [m for mid, m in materials.items() if mid not in entries and mid not in excluded]
        sections += ["Examiner's requests:", *[f"- {r.problem} ({r.suggestion})" for r in exam.recovery],
                     "", "Current plan:", *[describe_entry(e) for e in entries.values()],
                     "", "New materials:", *([describe_material(m, folders) for m in new] or ["- none"])]
    elif state.get("review") and not state["review"].approved:
        system += "\n" + REVISE
        review = state["review"]
        sections += [f"Reviewer: {review.summary}",
                     *[f"- [{i.severity}/{i.category}] {i.detail} {i.material_ids}" for i in review.issues],
                     "", "Current course map: " + (course_map[0].model_dump_json() if course_map[0] else "none"),
                     "", "Current plan:", *[describe_entry(e) for e in entries.values()],
                     "", "Excluded:", *[f"{x.material_id}: {x.reason}" for x in excluded.values()],
                     "", "All materials:", *[describe_material(m, folders) for m in materials.values()]]
    else:
        sections += [f"Materials found ({len(materials)}):", *[describe_material(m, folders) for m in materials.values()]]

    result = run_agent(role="tutor", name="Tutor", system=system, user="\n".join(sections), tools=tools, rc=rc)
    new_plan = Plan(entries=list(entries.values()), excluded=list(excluded.values()),
                    summary=result.final if result.finished else old_plan.summary)
    emit({"type": "plan", "agent": "Tutor", "planned": len(new_plan.entries), "excluded": len(new_plan.excluded),
          "modules": len(course_map[0].modules) if course_map[0] else 0, "finished": result.finished})
    update = {"plan": new_plan, "usage": {"tutor": result.tokens}}
    if course_map[0] is not None:
        update["course_map"] = course_map[0]
    elif not state.get("course_map"):
        update["course_map"] = CourseMap(title=brief.course_title, summary=brief.summary)
    if not result.finished:
        update["stop_reason"] = rc.stop_reason() or ""
    return update
