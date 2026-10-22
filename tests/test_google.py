from __future__ import annotations

import io
import re
import threading

from PIL import Image

from scraper.config import DRIVE_FOLDER_MAX_DEPTH
from scraper.engine import google
from scraper.engine.context import TaskContext
from scraper.identify import identify
from scraper.state import DownloadTask

FOLDER_VIEW = """
<html><head><title>Lecture Slides</title></head><body>
<div class="flip-entry" id="entry-1glql8SV6BVF"><a href="https://drive.google.com/file/d/1glql8SV6BVF/view?usp=drive_web">
  <div class="flip-entry-title">Lecture01_Intro.pdf</div></a></div>
<div class="flip-entry" id="entry-1fBXremcWVJS"><a href="https://docs.google.com/presentation/d/1fBXremcWVJS/edit?usp=drive_web">
  <div class="flip-entry-title">Week 1 deck</div></a></div>
<div class="flip-entry" id="entry-1HRxM3MXlJZ1"><a href="https://drive.google.com/drive/folders/1HRxM3MXlJZ1">
  <div class="flip-entry-title">Extra</div></a></div>
</body></html>
"""


def test_parse_folder_view():
    title, entries = google.parse_folder_view(FOLDER_VIEW)
    assert title == "Lecture Slides"
    assert [(e.id, e.title, e.is_folder) for e in entries] == [
        ("1glql8SV6BVF", "Lecture01_Intro.pdf", False),
        ("1fBXremcWVJS", "Week 1 deck", False),
        ("1HRxM3MXlJZ1", "Extra", True),
    ]


def _ctx(task: DownloadTask, tmp_path) -> TaskContext:
    return TaskContext(task=task, ref=identify(task.url), cancel=threading.Event(), tmp_dir=tmp_path,
                       _allocate=lambda folder, stem, key: tmp_path.joinpath(*folder, stem), _report=lambda *_: None)


def test_drive_folder_expands_into_child_tasks(tmp_path, monkeypatch):
    monkeypatch.setattr(google, "list_drive_folder", lambda _id: google.parse_folder_view(FOLDER_VIEW))
    task = DownloadTask(url="https://drive.google.com/drive/folders/ROOT123456", folder=["Week 1"])
    result = google.drive_folder(_ctx(task, tmp_path))

    assert result.files == []
    pdf, deck, sub = result.children
    assert pdf.folder == deck.folder == sub.folder == ["Week 1", "Lecture Slides"]
    assert (pdf.name, deck.name, sub.name) == ("Lecture01_Intro", "Week 1 deck", "Extra")
    assert all(c.depth == 1 and c.parent_id == task.task_id for c in result.children)


def test_drive_folder_stops_at_max_depth(tmp_path, monkeypatch):
    monkeypatch.setattr(google, "list_drive_folder", lambda _id: google.parse_folder_view(FOLDER_VIEW))
    task = DownloadTask(url="https://drive.google.com/drive/folders/ROOT123456", depth=DRIVE_FOLDER_MAX_DEPTH)
    result = google.drive_folder(_ctx(task, tmp_path))
    assert len(result.children) == 2  # the subfolder is dropped
    assert "skipped" in result.message


def _jpeg(color: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (160, 90), color).save(buf, "JPEG")
    return buf.getvalue()


def test_jpeg_size():
    assert google.jpeg_size(_jpeg("red")) == (160, 90)


def test_jpegs_to_pdf_writes_a_valid_multipage_pdf(tmp_path):
    out = tmp_path / "deck.pdf"
    google.jpegs_to_pdf([_jpeg("red"), _jpeg("blue"), _jpeg("green")], out)
    data = out.read_bytes()
    assert b"/Width 160 /Height 90" in data and b"/MediaBox [0 0 960 540]" in data
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    assert data.count(b"/Type /Page ") == 3
    # Every xref offset must point at the start of its object.
    xref_at = int(re.search(rb"startxref\n(\d+)", data).group(1))
    rows = data[xref_at:].split(b"\n")[3:]
    for number, row in enumerate(rows, start=1):
        if not row.endswith(b" n "):
            break
        offset = int(row[:10])
        assert data[offset:].startswith(b"%d 0 obj" % number)
