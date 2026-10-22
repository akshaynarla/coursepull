"""Scout: explores one area of the course site and records the materials it finds.

Several Scouts run in parallel, each in its own context window. Least privilege:
a Scout can read pages and record findings; it cannot download or write files.
"""

from __future__ import annotations

from urllib.parse import urlparse

from pydantic import BaseModel, Field

from ..identify import identify
from ..run_context import RunContext, emit
from ..state import Material, MaterialRole, ScoutAssignment, ScoutReport, SiteBrief
from .checks import default_title, doubts
from .loop import Tool, ToolError, run_agent
from .toolbox import LinkBook, web_tools

SYSTEM = """\
You are a Scout in CoursePull, a team of agents that collects the study materials of an \
open online course so a student can study it offline. You explore one area of the course \
website and record every resource a student taking this course would need or benefit from.

How to work:
- Read your start pages with fetch_page. Links come with ids (L1, L2, ...) and what is known \
about them: [kind/provider], the heading they sit under ("in:") and the text of their table \
row or list item ("row:"). Use that context to tell what a bare "slides" or "video" link is.
- Course sites often hide the schedule behind tabs. If fetch_page lists controls that may hide \
content (e.g. "Syllabus", "Lectures"), call reveal_page on that page.
- Follow links to other pages of the same course site when they likely hold materials for your \
area (a page per lecture or week, a readings page). Pages are a shared budget: be selective.
- Read only pages of the course site; reading other sites is refused. Links to GitHub, papers, \
blogs, videos and other sites are recorded with record_materials, never read.
- reveal_page clicks every tab of a page: never guess URLs for tabs ("?tab=...", "#faq").
- Record the materials of each listing right after reading it: to save cost, older listings are \
removed from your context (their link ids stay valid). To see a listing again, call the same \
tool with the same URL and offset; never alter a URL to get a fresh copy.
- If materials live in a public Google Drive folder, list it with list_drive_folder and record \
the files in it (or record the folder itself if you can't list it).

What to record (record_materials, using link ids):
- Lecture slides and notes, lecture recordings, readings (required and optional), assignments, \
problem sets, solutions, exams, project descriptions, code repositories, notebooks, datasets, \
the syllabus and the course calendar.
- Record in a few large batches, e.g. a whole week or a whole table per call.
- module: the week/lecture/module exactly as the page names it, e.g. "Week 3" or "Lecture 5".
- required: true if the page marks it as required or it is core lecture material, false if \
optional or further reading, null if unclear.
- title is optional: leave it out when the link text and its row already say what it is.
- Record links marked "login?" too if they are course materials; the Tutor decides about them.

Do NOT record: site navigation, social media, people's homepages or profiles, sponsors, tool or \
company homepages, calendar invites or meeting links, references unrelated to studying.
Careful: in a schedule, a guest speaker's or author's NAME usually links to their homepage or \
LinkedIn profile. That is not the lecture recording or the slides; skip it.

record_materials answers with warnings when a link looks wrong for its role (e.g. a profile \
page recorded as a video). Take them seriously: drop_materials what you recorded by mistake.

When your area is covered, call finish with leads (pages of the course site you saw but did \
not explore that may hold more materials) and notes (anything the team should know, e.g. \
"recordings are on Panopto behind a login").
"""


class Pick(BaseModel):
    link: str = Field(description="Link id, e.g. L12")
    role: MaterialRole
    module: str | None = Field(None, description="Week/lecture/module as the page names it")
    required: bool | None = Field(None, description="true = required/core, false = optional, null = unclear")
    title: str | None = Field(None, description="Optional short title; defaults to the link text and its row")
    note: str = ""


class RecordArgs(BaseModel):
    items: list[Pick]


class DropArgs(BaseModel):
    links: list[str] = Field(description="Link ids recorded by mistake")
    reason: str


class FinishArgs(BaseModel):
    leads: list[str] = Field(default_factory=list,
                             description="Course-site pages you saw but did not explore that may hold materials")
    notes: str = Field("", description="Anything the rest of the team should know")


class ScoutWorkspace:
    """What one Scout has found so far. The record/drop tools work on this."""

    def __init__(self, scout_id: str, book: LinkBook) -> None:
        self.scout_id = scout_id
        self.book = book
        self.found: dict[str, Material] = {}

    def record(self, args: RecordArgs) -> str:
        problems, warnings = [], []
        for pick in args.items:
            entry = self.book.get(pick.link)
            if entry is None:
                problems.append(f"{pick.link}: unknown link id")
                continue
            link, page = entry
            kind = identify(link.url).kind
            self.found[link.url] = Material(
                url=link.url, title=pick.title or default_title(link, pick.role), kind=kind, role=pick.role,
                module_hint=pick.module, required=pick.required,
                evidence=" | ".join(p for p in (link.context_path, link.snippet) if p)[:300],
                found_on=page, scout_id=self.scout_id, note=pick.note,
                within=self.book.drive_ancestors(page),
            )
            if concerns := doubts(link.url, pick.role, kind):
                warnings.append(f"{pick.link} ({link.text[:40]!r}): {'; '.join(concerns)}")
        message = f"recorded; {len(self.found)} materials so far"
        if problems:
            message += "\nproblems: " + "; ".join(problems)
        if warnings:
            message += "\nwarnings (drop_materials anything recorded by mistake):\n- " + "\n- ".join(warnings)
        return message

    def drop(self, args: DropArgs) -> str:
        dropped = 0
        for link_id in args.links:
            entry = self.book.get(link_id)
            if entry and self.found.pop(entry[0].url, None) is not None:
                dropped += 1
        return f"dropped {dropped}; {len(self.found)} materials remain"


def run_scout(assignment: ScoutAssignment, brief: SiteBrief, covered: list[str], rc: RunContext,
              course_url: str = "") -> dict:
    name = f"Scout {assignment.scout_id}"
    book = LinkBook()
    workspace = ScoutWorkspace(assignment.scout_id, book)
    visited: dict[str, str] = {}  # page url -> title

    site = course_hosts(course_url, assignment.start_urls)
    tools = web_tools(rc, book, name)
    for tool in tools:
        if tool.name in ("fetch_page", "reveal_page"):
            tool.fn = _tracking(_on_site(tool.fn, site), visited, rc)

    def record(args: RecordArgs) -> str:
        message = workspace.record(args)
        emit({"type": "found", "agent": name, "total": len(workspace.found)})
        return message

    tools += [
        Tool("record_materials", "Record course materials you found, by link id.", RecordArgs, record),
        Tool("drop_materials", "Remove materials you recorded by mistake.", DropArgs, workspace.drop),
        Tool("finish", "Finish your area: report leads and notes.", FinishArgs, lambda a: a, final=True),
    ]

    term = ([f"This site hosts several offerings (terms) of the course. Collect ONLY materials of: {brief.term}. "
             "Skip anything that belongs to another term, including course-wide documents of other terms.", ""]
            if brief.term else [])
    user = "\n".join([
        f"Course: {brief.course_title}",
        f"About: {brief.summary}",
        f"Site structure: {brief.structure}",
        "",
        *term,
        f"Your area: {assignment.area}",
        f"Brief: {assignment.brief}",
        "Start pages:", *[f"- {u}" for u in assignment.start_urls],
        "",
        "Pages other scouts already cover (don't redo them):",
        *([f"- {u}" for u in covered[:40]] or ["- none"]),
        f"Pages left in the run's budget: {rc.pages.max_pages - rc.pages.reads}",
    ])
    result = run_agent(role="scout", name=name, system=SYSTEM, user=user, tools=tools, rc=rc)
    final: FinishArgs | None = result.final if result.finished else None
    found = workspace.found
    report = ScoutReport(
        scout_id=assignment.scout_id, area=assignment.area, pages_visited=list(visited), found=len(found),
        leads=final.leads if final else [], notes=final.notes if final else "",
        stopped_early=result.stopped, tokens=result.tokens, page_titles=visited,
    )
    emit({"type": "scout_done", "agent": name, "found": len(found), "pages": len(visited),
          "stopped": result.stopped})
    return {"findings": list(found.values()), "reports": [report], "usage": {"scout": result.tokens}}


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def course_hosts(course_url: str, start_urls: list[str]) -> set[str]:
    """The sites a Scout may read: the course's own and those of its start pages."""
    return {_host(u) for u in [course_url, *start_urls] if u}


def _on_site(fn, site: set[str]):
    def wrapped(args):
        if _host(args.url) not in site:
            raise ToolError(f"{args.url} is not on the course site ({', '.join(sorted(site))}). Record links to "
                            "other sites with record_materials; don't read them.")
        return fn(args)

    return wrapped


def _tracking(fn, visited: dict[str, str], rc: RunContext):
    def wrapped(args):
        result = fn(args)
        visited.setdefault(args.url, rc.pages.title(args.url))
        return result

    return wrapped
