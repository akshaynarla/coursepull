"""The whole workflow, end to end, on a local course site with scripted agents."""

from __future__ import annotations

import threading

import pytest

from scraper.graph import run_course

from .scripted_agents import Brains


def _run(site, out, brains, events=None, **kwargs):
    sink = events.append if events is not None else None
    return run_course(f"{site}/course/", out, llm=brains.router(), on_event=sink, **kwargs)


def test_full_run_with_review_loop_and_recovery(site, tmp_path):
    brains, events = Brains(site), []
    state = _run(site, tmp_path, brains, events)

    assert state["phase"] == "finished" and not state.get("stop_reason")
    assert state["review_rounds"] == 2  # rejected once, then approved
    assert state["recovery_rounds"] == 1  # Paper B 404 -> mirror found by a recovery scout
    assert state["examination"].passed

    pdfs = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.pdf"))
    assert pdfs == [
        "01 - Intro/slides/Week 1 slides.pdf",
        "02 - Mocks/slides/Week 2 slides.pdf",
        "Course/readings/Paper A.pdf",
        "Course/readings/Paper B mirror.pdf",
    ]
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "# Testing 101" in readme and "## 01 · Intro" in readme
    assert "[pdf](<01 - Intro/slides/Week 1 slides.pdf>)" in readme
    assert "replaced by the mirror" in (tmp_path / "skipped.md").read_text(encoding="utf-8")

    phases = [e["phase"] for e in events if e["type"] == "phase"]
    for phase in ("discovery", "planning", "review", "download", "examination", "recovery", "finished"):
        assert phase in phases
    agents = {e["agent"] for e in events if e["type"] == "narration"}
    assert {"Orchestrator", "Scout A1", "Scout B1"} <= agents
    downloads = [e for e in events if e["type"] == "download" and e["event"] == "finished"]
    assert {e["status"] for e in downloads} == {"done", "failed"}
    assert events[-1]["type"] == "usage" and set(events[-1]["by_role"]) == {
        "orchestrator", "scout", "tutor", "reviewer", "examiner"}


def test_stop_ends_the_run_cleanly(site, tmp_path):
    brains, cancel, events = Brains(site), threading.Event(), []

    def on_event(event):
        events.append(event)
        if event["type"] == "page":
            cancel.set()  # the user presses Stop while Scouts are reading

    state = run_course(f"{site}/course/", tmp_path, llm=brains.router(), on_event=on_event, cancel=cancel)
    assert state["stop_reason"] == "cancelled"
    assert not list(tmp_path.rglob("*.pdf"))
    assert "stopped by the user" in (tmp_path / "README.md").read_text(encoding="utf-8")
    assert [e for e in events if e["type"] == "done"][0]["stop_reason"] == "cancelled"
    assert not any(e["type"] == "download" for e in events)


def test_token_budget_stops_the_run(site, tmp_path):
    state = _run(site, tmp_path, Brains(site), token_budget=450)
    assert state["stop_reason"] == "budget"
    assert (tmp_path / "README.md").exists()


def test_crashed_run_resumes_without_redoing_finished_work(site, tmp_path):
    brains = Brains(site)
    brains.fail_tutor_once = True
    with pytest.raises(RuntimeError, match="LLM endpoint is down"):
        _run(site, tmp_path, brains)
    assert sorted(brains.scouted) == ["readings", "schedule"]

    state = _run(site, tmp_path, brains)
    assert state["phase"] == "finished"
    # Discovery was not repeated; the only new scout is the recovery one.
    assert sorted(brains.scouted) == ["Paper B mirror", "readings", "schedule"]
    assert len(list(tmp_path.rglob("*.pdf"))) == 4


def test_a_finished_run_starts_fresh_next_time(site, tmp_path):
    _run(site, tmp_path, Brains(site))
    brains, events = Brains(site), []
    _run(site, tmp_path, brains, events)
    assert events[0]["type"] == "run" and not events[0]["resuming"]
    finished = [e for e in events if e["type"] == "download" and e["event"] == "finished" and e["status"] == "done"]
    assert finished  # files were found already on disk and reported as done


def test_plan_only_run_downloads_nothing(site, tmp_path):
    state = _run(site, tmp_path, Brains(site), plan_only=True)
    assert state["phase"] == "finished" and len(state["plan"].entries) == 4
    assert not state.get("outcomes")
    assert not list(tmp_path.rglob("*.pdf"))
    assert "plan-only run" in (tmp_path / "README.md").read_text(encoding="utf-8")


# Scripted turns cost 100 tokens each. Discovery (orchestrator 2 + scouts 6 + decide 1) is
# 900 tokens; the Tutor's first plan takes 3 more turns.


def test_past_the_soft_budget_the_run_winds_down_but_still_downloads(site, tmp_path):
    state = _run(site, tmp_path, Brains(site), token_budget=1250)  # soft limit 1000
    assert not state.get("review_rounds")  # review skipped
    assert not state.get("recovery_rounds")  # recovery skipped
    assert state["examination"] and not state["examination"].passed
    assert len(list(tmp_path.rglob("*.pdf"))) == 3  # Paper B failed and was not recovered


def test_budget_spent_mid_plan_still_downloads_the_partial_plan(site, tmp_path):
    state = _run(site, tmp_path, Brains(site), token_budget=1100)
    assert state["stop_reason"] == "budget"
    assert state["plan"].entries and not state.get("examination")
    assert len(list(tmp_path.rglob("*.pdf"))) == 3
    assert "token budget was used up" in (tmp_path / "README.md").read_text(encoding="utf-8")
