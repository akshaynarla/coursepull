"""The eval set: frozen course sites, answer keys and plan scores."""

from __future__ import annotations

import pytest

import scraper.agents.toolbox as toolbox
from scraper.agents.loop import ToolError
from scraper.agents.toolbox import PageStore, SavedPage, load_pages
from scraper.evals import EVALS_DIR, Expected, Truth, course_names, load_truth, match_key, run_eval, score_plan
from scraper.graph import run_course
from scraper.state import DownloadTask, Plan, PlanEntry, SiteBrief

from .scripted_agents import Brains

PAGE = "<html><head><title>Course</title></head><body><h2>Week 1</h2><a href='/l1.pdf'>slides</a></body></html>"


def _entry(url: str, module: str | None, role: str, name: str = "x") -> PlanEntry:
    return PlanEntry(material_id="M1", task=DownloadTask(url=url, name=name), module_key=module, role=role)


def test_saved_pages_are_read_like_live_ones_and_nothing_else_is(tmp_path, monkeypatch):
    store = PageStore(10, saved={"https://c.edu/": SavedPage("https://c.edu/", [("", PAGE)], False)})
    monkeypatch.setattr(toolbox, "fetch", lambda url: pytest.fail(f"read the live site: {url}"))
    snapshot = store.get("https://c.edu/#schedule")
    assert snapshot.outline.title == "Course" and [link.url for link in snapshot.links] == ["https://c.edu/l1.pdf"]
    with pytest.raises(ToolError, match="not in the saved copy"):
        store.get("https://c.edu/other")
    assert store.reads == 2


def test_pages_round_trip_and_a_revealed_copy_wins(tmp_path):
    path = tmp_path / "pages.json.gz"
    first = PageStore(10, saved={"https://c.edu/": SavedPage("https://c.edu/", [("", PAGE)], False)})
    first.get("https://c.edu/")
    first.save(path)
    second = PageStore(10)
    second._raw["https://c.edu/"] = SavedPage("https://c.edu/", [("", PAGE), ("Syllabus", PAGE)], True)
    second.save(path)  # adds to the file; the revealed copy replaces the plain one
    pages = load_pages(path)
    assert pages["https://c.edu/"].revealed and pages["https://c.edu/"].views[1] == ("Syllabus", PAGE)


def test_items_match_by_resource_whatever_the_link_form_or_branch():
    assert match_key("https://arxiv.org/abs/1803.07055") == match_key("https://arxiv.org/pdf/1803.07055")
    assert match_key("https://youtu.be/aG8NPTPhwkE") == match_key("https://www.youtube.com/watch?v=aG8NPTPhwkE")
    assert match_key("https://github.com/o/r/tree/master/week1") == match_key("https://github.com/o/r/tree/fall2025/week1")
    assert match_key("https://github.com/o/r/tree/master/week1") != match_key("https://github.com/o/r/tree/master/week2")


def test_a_plan_is_scored_against_the_answer_key():
    truth = Truth("t", "https://c.edu/", term="Fall 2025", items=[
        Expected("https://c.edu/l1.pdf", module="01", role="lecture_slides"),
        Expected("https://arxiv.org/abs/2001.00001", module="01", role="reading"),
        Expected("https://c.edu/l2.pdf", module="02", role="lecture_slides"),
        Expected("https://c.edu/syllabus.pdf", optional=True),
        Expected("https://c.edu/hw.zip"),  # module and role not scored
    ])
    state = {
        "plan": Plan(entries=[
            _entry("https://c.edu/l1.pdf", "01", "lecture_slides"),
            _entry("https://arxiv.org/pdf/2001.00001", "02", "other"),  # wrong module and role
            _entry("https://c.edu/hw.zip", None, "assignment"),
            _entry("https://c.edu/hw.zip?x", None, "assignment"),  # not the same resource: an extra
            _entry("https://linkedin.com/in/someone", "01", "lecture_video", name="Profile"),
        ]),
        "brief": SiteBrief(course_title="Testing", term="Fall 2026"),
        "usage": {"scout": 1000, "tutor": 500},
    }
    score = score_plan(truth, state)
    assert (score["required_found"], score["required"]) == (3, 4) and score["optional_found"] == 0
    assert [m["url"] for m in score["missing"]] == ["https://c.edu/l2.pdf"]
    assert {e["name"] for e in score["extras"]} == {"x", "Profile"} and score["precision"] == 3 / 5
    assert score["module_correct"] == 2 and score["wrong_module"][0]["expected"] == "01"
    assert score["role_correct"] == 2 and score["wrong_role"][0]["role"] == "other"
    assert score["term_ok"] is False and score["tokens"] == 1500


def test_an_item_may_accept_several_roles():
    truth = Truth("t", "https://c.edu/", items=[Expected("https://c.edu/hw1.zip", role=["assignment", "code"]),
                                                Expected("https://c.edu/hw2.zip", role=["assignment", "code"])])
    state = {"plan": Plan(entries=[_entry("https://c.edu/hw1.zip", None, "code"),
                                   _entry("https://c.edu/hw2.zip", None, "reading")])}
    score = score_plan(truth, state)
    assert score["role_correct"] == 1 and score["wrong_role"][0]["expected"] == "assignment or code"


def test_the_answer_keys_load_and_name_each_resource_once():
    assert {"robot-learning", "themodernsoftware-dev"} <= set(course_names())
    for name in course_names():
        truth = load_truth(name)
        keys = [match_key(item.url) for item in truth.items]
        assert len(keys) == len(set(keys)), name
        assert (EVALS_DIR / name / "pages.json.gz").exists(), name


def test_an_eval_run_plans_from_the_saved_site_only(site, tmp_path, monkeypatch):
    # A live plan-only run saves the pages its agents read...
    live = tmp_path / "live"
    state = run_course(f"{site}/course/", live, llm=Brains(site).router(), plan_only=True)
    pages = live / ".coursepull" / "pages.json.gz"
    assert pages.exists()
    evals = tmp_path / "evals" / "testing-101"
    evals.mkdir(parents=True)
    (evals / "pages.json.gz").write_bytes(pages.read_bytes())
    lines = [f'course_url = "{site}/course/"', ""]
    for entry in state["plan"].entries:
        lines += ["[[item]]", f'url = "{entry.task.url}"', f'module = "{entry.module_key or ""}"',
                  f'role = "{entry.role}"', ""]
    (evals / "truth.toml").write_text("\n".join(lines), encoding="utf-8")

    # ...and an eval run on them never touches the live site, and gets the same plan.
    monkeypatch.setattr(toolbox, "fetch", lambda url: pytest.fail(f"read the live site: {url}"))
    monkeypatch.setattr(toolbox, "render_interactive", lambda url: pytest.fail(f"read the live site: {url}"))
    score, run_dir = run_eval("testing-101", llm=Brains(site).router(), evals_dir=tmp_path / "evals",
                              runs_dir=tmp_path / "runs")
    assert score["recall"] == 1.0 and score["precision"] == 1.0 and not score["wrong_role"]
    assert (run_dir / "eval.md").read_text(encoding="utf-8").startswith("# Eval: testing-101")
