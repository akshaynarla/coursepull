"""The UI's view model, fed with the events of a full scripted run."""

from __future__ import annotations

import json

from scraper.graph import run_course
from scraper.ui.view import AGENTS, RunView, short_name

from .scripted_agents import Brains


def _scripted_run(site, out, **kwargs):
    events: list[dict] = []
    run_course(f"{site}/course/", out, llm=Brains(site).router(), on_event=events.append, **kwargs)
    return events


def test_view_follows_a_full_run(site, tmp_path):
    events = _scripted_run(site, tmp_path)
    view = RunView()
    agents_seen = []
    for event in events:
        view.apply(event)
        if not agents_seen or agents_seen[-1] != view.active:
            agents_seen.append(view.active)

    assert view.status == "finished" and view.title == "Testing 101"
    assert [heading for heading, _ in view.module_groups()] == ["01 · Intro", "02 · Mocks", "Course-wide"]
    names = {i.name: i.status for i in view.items.values()}
    assert names == {"Week 1 slides": "done", "Week 2 slides": "done", "Paper A": "done",
                     "Paper B mirror": "done"}  # Paper B itself was replaced by the mirror during recovery
    assert all(i.files for i in view.items.values())
    assert set(view.scouts) == {"Scout A1", "Scout B1", "Scout A2"}
    assert view.scouts["Scout A1"].done and view.scouts["Scout A1"].found == 2
    assert view.review["approved"] and view.examination["passed"]
    assert view.tokens_used > 0 and view.by_role
    assert view.summary["files"] == 4 and view.elapsed >= 0
    # The graph lit up every agent, and recovery went back to the Orchestrator.
    assert {"orchestrator", "scouts", "tutor", "reviewer", "engine", "examiner", "finish"} <= set(view.visited)
    assert agents_seen.index("examiner") < len(agents_seen) - 1 - agents_seen[::-1].index("orchestrator")
    kinds = {line.kind for line in view.lines}
    assert {"narration", "tool", "page", "phase", "info", "error"} <= kinds  # the Paper B 404 is an error line


def test_every_event_is_recorded_for_replay(site, tmp_path):
    events = _scripted_run(site, tmp_path, plan_only=True)
    recorded = [json.loads(line) for line in (tmp_path / ".coursepull" / "events.jsonl").read_text("utf-8").splitlines()]
    assert [e["type"] for e in recorded] == [e["type"] for e in events]
    assert all("ts" in e for e in recorded)
    replayed = RunView()
    for event in recorded:
        replayed.apply(event)
    assert replayed.status == "finished" and len(replayed.items) == 4
    assert all(i.status == "planned" for i in replayed.items.values())


def test_an_error_shows_in_the_view():
    view = RunView()
    view.apply({"type": "run", "course_url": "https://c.edu", "output_dir": "out", "resuming": False, "ts": 1})
    view.apply({"type": "error", "message": "AuthenticationError: 401", "ts": 2})
    assert view.status == "error" and view.error == "AuthenticationError: 401" and view.elapsed == 1


def test_the_agent_list_follows_the_run():
    view = RunView(status="running", active="tutor", visited=["orchestrator", "scouts", "tutor"])
    assert [view.agent_state(node) for node in AGENTS] == ["done", "done", "active"] + ["idle"] * 4
    assert view.agent_caption("reviewer") == AGENTS["reviewer"][1]  # not started: what it does
    view.review, view.by_role = {"approved": True}, {"reviewer": 9_197}
    assert view.agent_caption("reviewer") == "approved the plan · 9k tokens"
    view.status = "finished"
    assert view.agent_state("tutor") == "done"  # nothing is working once the run is over


def test_item_names_lose_the_prefix_their_module_card_shows():
    assert short_name("Week 02 - Reading - Simple Random Search", "02") == "Reading - Simple Random Search"
    assert short_name("Lecture 3: Imitation", "03") == "Imitation"
    assert short_name("Week 12 - Guest talk", "02") == "Week 12 - Guest talk"  # another module's number stays
    assert short_name("Syllabus", None) == "Syllabus"


def test_the_page_module_loads_and_its_helpers_work():
    from scraper.ui import page

    assert page.default_folder("https://themodernsoftware.dev/fall2025").replace("\\", "/") == \
        "downloads/themodernsoftware-dev-fall2025"
    assert page.fmt_bytes(1536) == "1.5 KB" and page.fmt_duration(75) == "1:15" and page.fmt_duration(3700) == "1h 01m"


def test_the_recorded_demo_run_replays_into_a_finished_view():
    from pathlib import Path

    events = Path(__file__).parent / "fixtures" / "scripted_run_events.jsonl"
    view = RunView()
    for line in events.read_text(encoding="utf-8").splitlines():
        view.apply(json.loads(line))
    assert view.status == "finished" and view.finished_items == len(view.items) == 4


def test_retry_downloads_only_what_failed_or_went_missing(site, tmp_path):
    from pathlib import Path

    from scraper.graph import build_graph, retry_failed
    from scraper.graph.run import checkpointer
    from scraper.state import ErrorKind, make_task_id

    url = f"{site}/course/"
    _scripted_run(site, tmp_path)
    saver, conn = checkpointer(tmp_path / ".coursepull" / "checkpoints.sqlite")
    app = build_graph().compile(checkpointer=saver)
    config = {"configurable": {"thread_id": make_task_id(url)}}
    state = app.get_state(config).values
    by_name = {e.task.name: e for e in state["plan"].entries}
    week1, week2 = by_name["Week 1 slides"], by_name["Week 2 slides"]
    # Simulate run 8: one item failed outright, another's file is gone from disk.
    failed = state["outcomes"][week2.task.task_id].model_copy(
        update={"status": "failed", "error_kind": ErrorKind.NETWORK, "message": "the page took too long to load"})
    app.update_state(config, {"outcomes": {week2.task.task_id: failed}}, as_node="finish")
    conn.close()
    for entry in (week1, week2):
        for f in state["outcomes"][entry.task.task_id].files:
            Path(f).unlink()

    events: list[dict] = []
    updated = retry_failed(url, tmp_path, on_event=events.append)
    retried = [e for e in events if e["type"] == "download" and e["event"] == "finished"]
    assert sorted(e["name"] for e in retried) == ["Week 1 slides", "Week 2 slides"]  # nothing else re-downloaded
    assert all(e["status"] == "done" for e in retried)
    assert updated["outcomes"][week2.task.task_id].status == "done"
    assert events[-1]["type"] == "done" and events[-1]["failed"] == 0
    assert "Failed downloads" not in (tmp_path / "skipped.md").read_text(encoding="utf-8")
    view = RunView()
    for event in events:
        view.apply(event)
    assert view.status == "finished" and len(view.items) == 2 and view.finished_items == 2
