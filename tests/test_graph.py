"""The whole workflow, end to end, on a local course site with scripted agents."""

from __future__ import annotations

import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pypdf import PdfWriter

from scraper.graph import run_course

from .scripted_agents import Brains

FILLER = "<p>" + "This course teaches practical software testing. " * 6 + "</p>" + '<a href="#top">top</a>' * 5


def _pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


SITE = {
    "/course/": f"""<html><head><title>Testing 101</title></head><body><h1>Testing 101</h1>
        <nav><a href="/course/readings.html">Readings</a> <a href="https://twitter.com/prof">Twitter</a></nav>
        <h2>Schedule</h2><table>
        <tr><td>Week 1</td><td>Intro</td><td><a href="/course/l1.pdf">Week 1 slides</a></td></tr>
        <tr><td>Week 2</td><td>Mocks</td><td><a href="/course/l2.pdf">Week 2 slides</a></td></tr>
        </table>{FILLER}</body></html>""",
    "/course/readings.html": f"""<html><head><title>Readings</title></head><body><h2>Required reading</h2>
        <ul><li><a href="/course/paper-a.pdf">Paper A</a></li>
        <li><a href="/course/paper-b.pdf">Paper B</a> (<a href="/course/mirror/paper-b.pdf">Paper B mirror</a>)</li>
        </ul>{FILLER}</body></html>""",
}
PDFS = {"/course/l1.pdf", "/course/l2.pdf", "/course/paper-a.pdf", "/course/mirror/paper-b.pdf"}


class _Site(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path in SITE:
            body, ctype = SITE[path].encode(), "text/html; charset=utf-8"
        elif path in PDFS:
            body, ctype = _pdf(), "application/pdf"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


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
