"""Regressions from run 8, the first full download of themodernsoftware.dev (Fall 2025)."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from pydantic import BaseModel

import scraper.agents.loop as agent_loop
from scraper.agents.loop import Tool, ToolError, run_agent
from scraper.agents.orchestrator import AssignmentArgs, _assignments
from scraper.engine import DownloadEngine, archive, files, webpage
from scraper.engine.context import HandlerResult, TaskContext
from scraper.engine.errors import DownloadError
from scraper.engine.manifest import Manifest
from scraper.identify import identify
from scraper.llm import LLMRouter, ToolCall, Turn
from scraper.run_context import RunContext
from scraper.state import DownloadTask, ErrorKind, Review

REPO = "https://github.com/mihail911/modern-software-dev-assignments"


def _ctx(url: str, tmp_path: Path, term: str = "") -> TaskContext:
    task = DownloadTask(url=url, term=term)
    return TaskContext(task=task, ref=identify(url), cancel=threading.Event(), tmp_dir=tmp_path,
                       _allocate=lambda folder, stem, key: tmp_path / stem, _report=lambda *_: None)


# ---------- web pages that never go "network idle" (4 failures) ----------

@pytest.mark.parametrize(("message", "kind"), [
    ("Page.goto: net::ERR_NAME_NOT_RESOLVED at https://blog.stockapp.com/x\nCall log: ...", ErrorKind.NOT_FOUND),
    ("Page.goto: net::ERR_CONNECTION_REFUSED at https://x", ErrorKind.NOT_FOUND),
    ("Page.goto: Timeout 30000ms exceeded.\nCall log:", ErrorKind.NETWORK),
    ("Something else broke", ErrorKind.UNKNOWN),
])
def test_page_load_errors_are_typed(message, kind):
    assert webpage.browser_error(RuntimeError(message)).kind == kind


class _BusyPage(BaseHTTPRequestHandler):
    """A page that keeps the network busy forever, like analytics or chat widgets do."""

    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:
        if self.path.startswith("/poll"):
            body, ctype = b"{}", "application/json"
        else:
            body = (b"<html><head><title>Busy article</title></head><body><h1>Busy article</h1>"
                    b"<p>Text worth keeping.</p><script>setInterval(() => fetch('/poll?' + Math.random()), 300);"
                    b"</script></body></html>")
            ctype = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.mark.network  # needs Edge
def test_a_page_that_never_goes_idle_is_still_saved(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BusyPage)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        started = time.time()
        path = webpage.save_page_pdf(_ctx(f"http://127.0.0.1:{server.server_address[1]}/article", tmp_path))
        assert path.read_bytes()[:4] == b"%PDF" and time.time() - started < 25  # it used to time out at 30 s
    finally:
        server.shutdown()
        server.server_close()


# ---------- dead links: the Internet Archive's copy ----------

def test_private_addresses_are_never_looked_up(monkeypatch):
    monkeypatch.setattr(archive, "session", lambda: pytest.fail("no lookup for local addresses"))
    for url in ("http://127.0.0.1:8000/a.pdf", "http://localhost/x", "http://192.168.1.4/x", "http://10.0.0.2/x"):
        assert archive.archived_timestamp(url) is None


def test_archive_lookup(monkeypatch):
    class Resp:
        def json(self):
            return {"archived_snapshots": {"closest": {"available": True, "status": "200",
                                                       "timestamp": "20250611083000"}}}

    monkeypatch.setattr(archive, "session", lambda: type("S", (), {"get": lambda *a, **k: Resp()})())
    stamp = archive.archived_timestamp("https://blog.stockapp.com/good-context-good-code/")
    assert stamp == "20250611083000"
    assert archive.page_url(stamp, "https://x.com/a") == "https://web.archive.org/web/20250611083000if_/https://x.com/a"
    assert archive.raw_url(stamp, "https://x.com/a.pdf").endswith("20250611083000id_/https://x.com/a.pdf")
    assert archive.note(stamp).endswith("copy from 2025-06-11")


def test_a_dead_page_is_saved_from_the_archive(tmp_path, monkeypatch):
    url = "https://blog.stockapp.com/good-context-good-code/"
    saved = []

    def fake_save(ctx, target=None):
        if target in (None, url):  # the page itself
            raise DownloadError(ErrorKind.NOT_FOUND, "site unreachable (ERR_NAME_NOT_RESOLVED)")
        saved.append(target)
        return tmp_path / "page.pdf"

    monkeypatch.setattr(webpage, "save_page_pdf", fake_save)
    monkeypatch.setattr(archive, "archived_timestamp", lambda u: "20250611083000")
    result = webpage.save_webpage(_ctx(url, tmp_path))
    assert saved == [archive.page_url("20250611083000", url)]
    assert result.message == "the original is gone; saved the Internet Archive's copy from 2025-06-11"


def test_a_dead_file_is_downloaded_from_the_archive(tmp_path, monkeypatch):
    url = "https://old.example.edu/notes/lecture3.pdf"
    fetched = []

    def fake_download(target, claim, **_):
        fetched.append(target)
        if target == url:
            raise DownloadError(ErrorKind.NOT_FOUND, "HTTP 404: not found")
        return tmp_path / "lecture3.pdf"

    monkeypatch.setattr(files, "download", fake_download)
    monkeypatch.setattr(archive, "archived_timestamp", lambda u: "20240101000000")
    result = files.download_file(_ctx(url, tmp_path))
    assert fetched == [url, archive.raw_url("20240101000000", url)] and "Internet Archive" in result.message


# ---------- GitHub files planned as "web pages" bypassed the term branch (Week 3/4/6) ----------

def test_a_github_file_planned_as_a_page_still_goes_to_the_repo_downloader(tmp_path, monkeypatch):
    from scraper.engine import engine as engine_module

    seen = []
    monkeypatch.setitem(engine_module.HANDLERS, identify(REPO).kind,
                        lambda ctx: seen.append(ctx.ref.kind.value) or HandlerResult([]))
    with DownloadEngine(tmp_path) as engine:
        engine.submit([DownloadTask(url=f"{REPO}/blob/master/week6/assignment.md", kind="webpage")])
        engine.wait(timeout=30)
    assert seen == ["repo"]


# ---------- bookkeeping must not fail a finished download ("Lecture 08", files on disk) ----------

def test_manifest_save_waits_out_a_brief_windows_lock(tmp_path, monkeypatch):
    failures = [PermissionError("[WinError 5] Access is denied")] * 2
    real_replace = Path.replace

    def flaky_replace(self, target):
        if failures:
            raise failures.pop()
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    (tmp_path / "a.pdf").write_bytes(b"%PDF")
    Manifest(tmp_path, tmp_path / ".s").record("k", "https://x/a.pdf", [tmp_path / "a.pdf"])
    assert json.loads((tmp_path / ".s" / "manifest.json").read_text())["k"]["files"] == ["a.pdf"]


def test_a_manifest_failure_does_not_fail_the_download(tmp_path, file_server, monkeypatch):
    def broken(*_args, **_kwargs):
        raise PermissionError("[WinError 5] Access is denied")

    monkeypatch.setattr(Manifest, "record", broken)
    with DownloadEngine(tmp_path) as engine:
        (task_id,) = engine.submit([DownloadTask(url=f"{file_server}/lecture8.pdf", name="Lecture 08")])
        engine.wait(timeout=30)
        outcome = engine.outcomes()[task_id]
    assert outcome.status == "done" and Path(outcome.files[0]).exists()


# ---------- agents hand in their work before the cap (the Examiner never submitted) ----------

class _NoArgs(BaseModel):
    pass


class _Investigator:
    """Keeps inspecting until told to wrap up, then submits."""

    model = "fake"

    def __init__(self):
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        if any(m["role"] == "user" and "nearly used up" in m["content"] for m in messages):
            return Turn("", [ToolCall("f", "submit_review", json.dumps({"approved": True, "summary": "partial"}))], 40)
        return Turn("", [ToolCall(f"c{self.calls}", "look", "{}")], 40)


def test_an_agent_is_told_to_hand_in_before_its_cap(tmp_path, monkeypatch):
    monkeypatch.setitem(agent_loop.AGENT_MAX_TOKENS, "reviewer", 200)
    monkeypatch.setitem(agent_loop.TOOL_POLICY, "reviewer", {"look", "submit_review"})
    rc = RunContext(output_dir=tmp_path, llm=LLMRouter({"reviewer": _Investigator()}))
    tools = [Tool("look", "inspect something", _NoArgs, lambda a: "x" * 50),
             Tool("submit_review", "submit", Review, lambda r: r, final=True)]
    result = run_agent(role="reviewer", name="Reviewer", system="s", user="u", tools=tools, rc=rc, max_steps=20)
    # Warned at 160 of 200 tokens, it handed in its work instead of being cut off empty-handed.
    assert result.finished and result.final.summary == "partial" and result.stopped == ""


# ---------- Scouts go to course pages only (Scout B1 crawled GitHub for 200k tokens) ----------

@pytest.mark.parametrize("url", [REPO, "https://www.youtube.com/watch?v=7xTGNNLPyMI",
                                 "https://arxiv.org/abs/2405.13565", "https://github.com/mihail911",
                                 "https://drive.google.com/drive/folders/1aEHCXzZKkokuA4AYjD_2i6OrcM5iQosW"])
def test_scouts_are_not_sent_to_repos_videos_papers_or_drive(url):
    with pytest.raises(ToolError, match="not a course web page"):
        _assignments([AssignmentArgs(area="x", start_urls=[url], brief="b")], wave=1)


def test_scouts_may_start_on_course_pages():
    (a,) = _assignments([AssignmentArgs(area="x", start_urls=["https://themodernsoftware.dev/fall2025"],
                                        brief="b")], wave=1)
    assert a.scout_id == "A1"


@pytest.mark.network
def test_live_archive_has_a_known_page():
    assert archive.archived_timestamp("https://example.com/")


def test_handler_result_messages_default_empty():
    assert HandlerResult([]).message == ""
