"""Guardrails around the agents: doubts, default titles, homepage coverage, least privilege.

The fixture is the material list from a real plan-only run (Qwen3.6-35B-A3B on
themodernsoftware.dev), in which seven guest speakers' profile pages were planned
as "lecture videos" and nobody objected.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scraper.agents.checks import default_title, doubts
from scraper.agents.loop import Tool, run_agent
from scraper.agents.orchestrator import cover_homepage
from scraper.agents.policy import TOOL_POLICY, UNTRUSTED_CONTENT
from scraper.agents.reviewer import automatic_checks
from scraper.agents.scout import DropArgs, Pick, RecordArgs, ScoutWorkspace
from scraper.agents.toolbox import LinkBook, PageArgs
from scraper.llm import LLMRouter, ToolCall, Turn
from scraper.models import Link
from scraper.run_context import RunContext
from scraper.state import DownloadTask, Plan, PlanEntry, Review, ScoutAssignment
from scraper.state import ResourceKind as K

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "modernsw_hf_materials.json").read_text(encoding="utf-8"))
REAL = {m["id"]: m for m in FIXTURE["materials"]}
SPEAKER_PROFILES = {"M29", "M39", "M46", "M57", "M66", "M70", "M80"}


def test_the_real_runs_speaker_profiles_are_all_flagged():
    flagged = {mid for mid, m in REAL.items() if doubts(m["url"], m["role"])}
    assert SPEAKER_PROFILES <= flagged


def test_the_real_runs_flags_are_exactly_the_questionable_items():
    flagged = {mid: doubts(m["url"], m["role"]) for mid, m in REAL.items()}
    flagged = {mid: d for mid, d in flagged.items() if d}
    whole_repos = {"M10", "M12", "M33", "M34"}  # repositories planned as readings
    tweet = {"M24"}  # an assigned post on x.com: flagged, the Tutor decides
    assert set(flagged) == SPEAKER_PROFILES | whole_repos | tweet
    assert all("whole code repository" in " ".join(flagged[m]) for m in whole_repos)
    # Real lecture recordings, slides, papers and assignments raise nothing.
    for clean in ("M1", "M3", "M8", "M17", "M47", "M60", "M63"):
        assert clean not in flagged


def test_default_title_uses_the_row_for_generic_link_text():
    bare = Link(url="https://c.edu/l3.pdf", text="slides", snippet="Week 3 Policy Gradients slides video")
    assert default_title(bare, "lecture_slides") == "Week 3 Policy Gradients slides video (slides)"
    named = Link(url="https://youtu.be/x", text="Deep Dive into LLMs")
    assert default_title(named, "lecture_video") == "Deep Dive into LLMs"


def _workspace():
    book = LinkBook()
    ids = [book.add(Link(url=url, text=text, snippet="Fri 10/17: Boris Cherney guest lecture slides"), "https://c.edu")
           for url, text in [("https://www.linkedin.com/in/someone", "Boris Cherney"),
                             ("https://docs.google.com/presentation/d/1AbCdEfGhIjK/edit", "slides")]]
    return ScoutWorkspace("A1", book), ids


def test_scout_is_warned_about_a_profile_recorded_as_a_video_and_can_drop_it():
    workspace, (profile, deck) = _workspace()
    reply = workspace.record(RecordArgs(items=[Pick(link=profile, role="lecture_video", module="Week 4"),
                                               Pick(link=deck, role="lecture_slides", module="Week 4"),
                                               Pick(link="L99", role="reading")]))
    assert "warnings" in reply and f"{profile} ('Boris Cherney')" in reply and "social or profile" in reply
    assert "not a video" in reply and "L99: unknown link id" in reply
    assert deck not in reply.split("warnings")[1]  # the slides deck is fine
    assert len(workspace.found) == 2
    assert workspace.drop(DropArgs(links=[profile], reason="speaker profile")) == "dropped 1; 1 materials remain"
    (kept,) = workspace.found.values()
    assert kept.title == "Fri 10/17: Boris Cherney guest lecture slides (slides)"


def test_homepage_is_always_covered():
    a = ScoutAssignment(scout_id="A1", area="schedule", start_urls=["https://c.edu/fall2025"], brief="slides")
    b = ScoutAssignment(scout_id="B1", area="readings", start_urls=["https://c.edu/readings"], brief="papers")
    widened = cover_homepage([a, b], "https://c.edu/")
    assert widened[0].start_urls == ["https://c.edu/", "https://c.edu/fall2025"]
    assert "calendar" in widened[0].brief and widened[1] == b
    already = ScoutAssignment(scout_id="A1", area="all", start_urls=["https://c.edu"], brief="x")
    assert cover_homepage([already], "https://c.edu/") == [already]


def _entry(mid: str, url: str, role: str, kind: K, module: str | None = "01") -> PlanEntry:
    return PlanEntry(material_id=mid, role=role, module_key=module,
                     task=DownloadTask(url=url, kind=kind, folder=["01 - Intro"], name=mid))


def test_reviewer_gets_the_doubts_as_automatic_checks():
    plan = Plan(entries=[
        _entry("M1", "https://docs.google.com/presentation/d/1AbCdEfGhIjK/edit", "lecture_slides", K.GOOGLE_SLIDES),
        _entry("M2", "https://www.linkedin.com/in/someone", "lecture_video", K.WEBPAGE),
        _entry("M3", "https://github.com/org/big-repo", "reading", K.REPO),
        _entry("M4", "https://github.com/org/other-repo", "reading", K.WEBPAGE),  # README page: fine
    ])
    notes = automatic_checks({"plan": plan, "materials": {}, "course_map": None})
    assert any("social or profile page" in n and "M2" in n for n in notes)
    assert any("not a video" in n and "M2" in n for n in notes)
    assert any("whole code repository" in n and n.endswith("['M3']") for n in notes)


class _Recorder:
    model = "fake"

    def __init__(self):
        self.system = ""

    def complete(self, messages, tools):
        self.system = messages[0]["content"]
        return Turn(content="", tool_calls=[ToolCall("c1", "submit_review", json.dumps(
            {"approved": True, "summary": "ok"}))], tokens=10)


def test_an_agent_cannot_be_given_a_tool_outside_its_role(tmp_path):
    rc = RunContext(output_dir=tmp_path, llm=LLMRouter({"reviewer": _Recorder()}))
    web = Tool("fetch_page", "read a page", PageArgs, lambda a: "")
    with pytest.raises(PermissionError, match="reviewer role may not use \\['fetch_page'\\]"):
        run_agent(role="reviewer", name="Reviewer", system="s", user="u", tools=[web], rc=rc)


def test_every_agent_is_told_that_page_content_is_untrusted(tmp_path):
    recorder = _Recorder()
    rc = RunContext(output_dir=tmp_path, llm=LLMRouter({"reviewer": recorder}))
    submit = Tool("submit_review", "submit", Review, lambda r: r, final=True)
    result = run_agent(role="reviewer", name="Reviewer", system="Review it.", user="u", tools=[submit], rc=rc)
    assert result.finished and recorder.system.endswith(UNTRUSTED_CONTENT)


def test_policy_keeps_web_and_file_tools_apart():
    assert not TOOL_POLICY["tutor"] & {"fetch_page", "reveal_page", "inspect_file"}
    assert TOOL_POLICY["reviewer"] == {"submit_review"}
    assert "fetch_page" not in TOOL_POLICY["examiner"]


# ---------- cost controls and terms (from the second real run) ----------

from pydantic import ValidationError  # noqa: E402

import scraper.agents.loop as agent_loop  # noqa: E402
from scraper.agents.loop import ToolError  # noqa: E402
from scraper.agents.orchestrator import AssignmentArgs, _assignments, consolidate  # noqa: E402
from scraper.agents.tutor import PlanItemArgs  # noqa: E402
from scraper.state import Material, SiteBrief  # noqa: E402


def test_a_later_wave_may_not_rescout_pages_already_read():
    visited = {"https://c.edu/fall2025": "A1"}
    again = AssignmentArgs(area="syllabus tab", start_urls=["https://C.edu/fall2025/"], brief="all of it")
    with pytest.raises(ToolError, match="already read.*Scout A1"):
        _assignments([again], wave=2, visited=visited)
    fresh = AssignmentArgs(area="readings", start_urls=["https://c.edu/readings"], brief="papers")
    assert _assignments([fresh], wave=2, visited=visited)[0].scout_id == "A2"


class _Chatty:
    model = "fake"

    def complete(self, messages, tools):
        return Turn(content="thinking out loud", tool_calls=[], tokens=20)


def test_an_agent_stops_at_its_own_token_cap(tmp_path, monkeypatch):
    monkeypatch.setitem(agent_loop.AGENT_MAX_TOKENS, "reviewer", 15)
    rc = RunContext(output_dir=tmp_path, llm=LLMRouter({"reviewer": _Chatty()}))
    submit = Tool("submit_review", "submit", Review, lambda r: r, final=True)
    result = run_agent(role="reviewer", name="Reviewer", system="s", user="u", tools=[submit], rc=rc)
    assert result.stopped == "token_limit" and result.tokens == 20 and rc.budget.used == 20


def test_the_tutor_must_decide_required_for_every_item():
    with pytest.raises(ValidationError, match="required"):
        PlanItemArgs(material="M1", name="Lecture 1", rationale="core")


def _material(url: str, page: str) -> Material:
    return Material(url=url, title=url, kind=K.FILE, role="lecture_slides", found_on=page)


def test_terms_stay_traceable_from_scout_findings_to_the_reviewer():
    shared = "https://c.edu/papers/attention.pdf"
    state = {"findings": [_material("https://c.edu/fa26/l1.pdf", "https://c.edu/"),
                          _material(shared, "https://c.edu/"),
                          _material(shared, "https://c.edu/fall2025"),
                          _material("https://c.edu/fa25/l1.pdf", "https://c.edu/fall2025")]}
    materials = consolidate(state)
    assert len(materials) == 3
    assert materials["M2"].found_on == "https://c.edu/ | https://c.edu/fall2025"  # listed in both terms

    plan = Plan(entries=[_entry(mid, m.url, "lecture_slides", K.FILE) for mid, m in materials.items()])
    notes = automatic_checks({"plan": plan, "materials": materials, "course_map": None,
                              "brief": SiteBrief(course_title="c", term="Fall 2025")})
    per_page = next(n for n in notes if n.startswith("planned items per source page"))
    assert "https://c.edu/ 2" in per_page and "https://c.edu/fall2025 2" in per_page


# ---------- Drive folders (run 4 planned the Skills folder and the files inside it) ----------

from scraper.agents.toolbox import LinkBook as _LinkBook  # noqa: E402

SKILLS = "https://drive.google.com/drive/folders/1aEHCXzZKkokuA4AYjD_2i6OrcM5iQosW?usp=drive_link"
EXPLORE = "https://drive.google.com/drive/folders/1wwep50YulmTmnhhxpxcl-HvnPJMc7CkO"
SKILL_MD = "https://drive.google.com/file/d/1UPI_mlJeviZf7-n6Ls4B4XyrDhyJ04m1/view?usp=drive_web"


def test_files_know_every_drive_folder_they_sit_in():
    book = _LinkBook()
    book.add(Link(url=SKILLS, text="Skills"), "https://themodernsoftware.dev/")
    book.add(Link(url=EXPLORE, text="explore"), SKILLS)  # listed inside Skills
    book.add(Link(url=SKILL_MD, text="SKILL.md"), EXPLORE)  # listed inside explore
    assert book.drive_ancestors(EXPLORE) == ["google:1wwep50YulmTmnhhxpxcl-HvnPJMc7CkO",
                                             "google:1aEHCXzZKkokuA4AYjD_2i6OrcM5iQosW"]
    assert book.drive_ancestors("https://themodernsoftware.dev/") == []


def test_reviewer_flags_files_inside_a_planned_folder():
    skills = Material(material_id="M15", url=SKILLS, title="Skills", kind=K.DRIVE_FOLDER, role="code")
    skill = Material(material_id="M34", url=SKILL_MD, title="SKILL.md", kind=K.DRIVE_FILE, role="code",
                     within=["google:1wwep50YulmTmnhhxpxcl-HvnPJMc7CkO", "google:1aEHCXzZKkokuA4AYjD_2i6OrcM5iQosW"])
    plan = Plan(entries=[_entry("M15", SKILLS, "code", K.DRIVE_FOLDER), _entry("M34", SKILL_MD, "code", K.DRIVE_FILE)])
    notes = automatic_checks({"plan": plan, "materials": {"M15": skills, "M34": skill}, "course_map": None})
    assert "planned files that sit inside a planned Drive folder (would download twice): ['M34']" in notes


# ---------- efficiency (run 5: five Scouts on one page, 43 off-site reads, every Scout at its cap) ----------

from scraper.agents.loop import MASKED, mask_old_results  # noqa: E402
from scraper.agents.scout import _on_site, course_hosts  # noqa: E402


def test_two_scouts_may_not_start_on_the_same_page():
    split = [AssignmentArgs(area="slides", start_urls=["https://c.edu/fall2025"], brief="decks"),
             AssignmentArgs(area="readings", start_urls=["https://c.edu/fall2025#syllabus"], brief="papers")]
    with pytest.raises(ToolError, match="two Scouts would start on the same page.*merge"):
        _assignments(split, wave=1)


def test_scouts_read_only_the_course_site():
    site = course_hosts("https://themodernsoftware.dev/", ["https://themodernsoftware.dev/fall2025"])
    read = _on_site(lambda args: "PAGE", site)
    assert read(PageArgs(url="https://www.themodernsoftware.dev/fall2025")) == "PAGE"
    for off_site in ("https://api.github.com/repos/x/y/contents/", "https://github.com/mihail911",
                     "https://raw.githubusercontent.com/x/y/master/week1/assignment.md"):
        with pytest.raises(ToolError, match="not on the course site.*record_materials"):
            read(PageArgs(url=off_site))


def _conversation(results: list[str]) -> list[dict]:
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    for i, content in enumerate(results):
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "fetch_page", "arguments": "{}"}}]})
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": content})
    return messages


def test_only_the_newest_page_listings_stay_in_context():
    listings = [f"PAGE {i} " + "x" * 5000 for i in range(4)]
    messages = _conversation([listings[0], "recorded; 12 materials so far", *listings[1:]])
    assert mask_old_results(messages, keep=2) == 2
    tool_results = [m["content"] for m in messages if m["role"] == "tool"]
    assert tool_results[0] == MASKED.format(tool="fetch_page", chars=5007, what=": PAGE 0 " + "x" * 153)
    assert "call fetch_page again with the same arguments" in tool_results[0]
    assert tool_results[1] == "recorded; 12 materials so far"  # small results are kept
    assert tool_results[2].startswith("[earlier fetch_page result removed")
    assert tool_results[3:] == listings[2:]
    assert set().union(*(m.keys() for m in messages)) <= {"role", "content", "tool_calls", "tool_call_id"}
    assert mask_old_results(messages, keep=2) == 0  # stable: nothing more to mask


def test_a_masked_page_listing_says_what_it_was():
    listing = "\n".join(["PAGE https://c.edu/fall2025", "title: Course",
                         "links 1-45 of 99 (more: call again with offset=45):", *["L1 x"] * 900])
    messages = _conversation([listing, "y" * 3000, "z" * 3000, "w" * 3000])
    mask_old_results(messages)
    stub = next(m["content"] for m in messages if m["role"] == "tool")
    assert ": PAGE https://c.edu/fall2025, links 1-45 of 99. Its ids stay valid" in stub


from scraper.agents.orchestrator import covered_pages  # noqa: E402
from scraper.state import ScoutReport  # noqa: E402


def test_pages_of_a_scout_that_failed_may_be_scouted_again():
    finished = ScoutReport(scout_id="A1", area="schedule", found=40, pages_visited=["https://c.edu/fall2025"])
    failed = ScoutReport(scout_id="B1", area="readings", found=0, stopped_early="token_limit",
                         pages_visited=["https://c.edu/readings", "https://c.edu/fall2025"])
    done, retry = covered_pages([finished, failed])
    assert done == {"https://c.edu/fall2025": "A1"}
    assert retry == ["https://c.edu/readings"]
    # Run 6: the only Scout failed, so its page must not block a second attempt.
    done, retry = covered_pages([ScoutReport(scout_id="A1", area="all", found=0, stopped_early="token_limit",
                                             pages_visited=["https://c.edu/fall2025"])])
    assert done == {} and retry == ["https://c.edu/fall2025"]
    _assignments([AssignmentArgs(area="all", start_urls=["https://c.edu/fall2025"], brief="b")], wave=2,
                 visited=done)
