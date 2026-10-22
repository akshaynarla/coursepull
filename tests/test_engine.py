from __future__ import annotations

import threading
from pathlib import Path

from scraper.engine import DownloadEngine
from scraper.state import DownloadTask, ErrorKind, ResourceKind


def _run(output_dir: Path, tasks: list[DownloadTask], events: list | None = None):
    sink = events.append if events is not None else None
    with DownloadEngine(output_dir, workers=4, on_event=sink) as engine:
        ids = engine.submit(tasks)
        engine.wait(timeout=30)
        outcomes = engine.outcomes()
    return ids, outcomes


def test_downloads_files_and_classifies_failures(tmp_path, file_server):
    events: list = []
    ids, outcomes = _run(tmp_path, [
        DownloadTask(url=f"{file_server}/lecture1.pdf", folder=["Week 1", "slides"], name="Lecture 1: Intro"),
        DownloadTask(url=f"{file_server}/notes", kind=ResourceKind.FILE, folder=["Week 1"]),
        DownloadTask(url=f"{file_server}/login", kind=ResourceKind.FILE),
        DownloadTask(url=f"{file_server}/missing.pdf"),
    ], events)

    lecture, notes, login, missing = (outcomes[i] for i in ids)
    assert lecture.status == "done"
    assert Path(lecture.files[0]) == tmp_path / "Week 1" / "slides" / "Lecture 1 - Intro.pdf"
    assert Path(lecture.files[0]).read_bytes().startswith(b"%PDF")
    # No agent-given name: the server's Content-Disposition name is used, sanitised.
    assert notes.status == "done" and Path(notes.files[0]).name == "Week 1 - Notes.pdf"
    assert login.status == "failed" and login.error_kind == ErrorKind.AUTH_REQUIRED
    assert missing.status == "failed" and missing.error_kind == ErrorKind.NOT_FOUND

    lecture_events = [e.type for e in events if e.task_id == ids[0]]
    assert lecture_events[0] == "queued" and lecture_events[1] == "started" and lecture_events[-1] == "finished"


def test_same_name_in_same_folder_gets_unique_paths(tmp_path, file_server):
    ids, outcomes = _run(tmp_path, [
        DownloadTask(url=f"{file_server}/a.pdf", name="Lecture"),
        DownloadTask(url=f"{file_server}/b.pdf", name="Lecture"),
    ])
    names = sorted(Path(outcomes[i].files[0]).name for i in ids)
    assert names == ["Lecture (2).pdf", "Lecture.pdf"]


def test_duplicate_resource_is_queued_once(tmp_path, file_server):
    with DownloadEngine(tmp_path) as engine:
        first = engine.submit([DownloadTask(url=f"{file_server}/a.pdf")])
        second = engine.submit([DownloadTask(url=f"{file_server}/a.pdf", name="other name")])
        engine.wait(timeout=30)
        assert first == second
        assert engine.stats()["total"] == 1


def test_rerun_skips_finished_downloads(tmp_path, file_server):
    task = DownloadTask(url=f"{file_server}/a.pdf", name="Lecture")
    _, first = _run(tmp_path, [task])
    _, second = _run(tmp_path, [task])
    (a,), (b,) = first.values(), second.values()
    assert b.message == "already downloaded"
    assert a.files == b.files


def test_failed_task_can_be_resubmitted_with_another_kind(tmp_path, file_server):
    with DownloadEngine(tmp_path) as engine:
        (first,) = engine.submit([DownloadTask(url=f"{file_server}/notes", kind=ResourceKind.PAPER)])
        engine.wait(timeout=30)
        assert engine.outcomes()[first].status == "failed"
        (retry,) = engine.submit([DownloadTask(url=f"{file_server}/notes", kind=ResourceKind.FILE)])
        engine.wait(timeout=30)
        assert retry != first
        assert engine.outcomes()[retry].status == "done"


def test_cancel_stops_work_and_cleans_up(tmp_path, file_server):
    started = threading.Event()

    def on_event(event):
        if event.type == "progress":
            started.set()

    engine = DownloadEngine(tmp_path, on_event=on_event)
    (task_id,) = engine.submit([DownloadTask(url=f"{file_server}/slow.bin", kind=ResourceKind.FILE)])
    assert started.wait(timeout=10)
    engine.cancel()
    engine.close()

    outcome = engine.outcomes()[task_id]
    assert outcome.status == "cancelled" and outcome.error_kind == ErrorKind.CANCELLED
    assert not list(tmp_path.rglob("*.part"))
    assert not engine.tmp_dir.exists()
