"""End-to-end checks against real sites. Run with: pytest --run-network -m network

These hit Google, GitHub, arxiv and YouTube, so they are slower and can fail if
a site changes or a shared file is taken down.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from scraper.engine import DownloadEngine, list_drive_folder
from scraper.extract import extract_views
from scraper.fetch import render_interactive
from scraper.identify import identify
from scraper.state import DownloadTask, ResourceKind

pytestmark = pytest.mark.network

MODERN_SW = "https://themodernsoftware.dev/fall2025"
SLIDES = "https://docs.google.com/presentation/d/11CP26VhsjnZOmi9YFgLlonzdib9BLyAlgc4cEvC5Fps/edit?usp=sharing"
PUBLISHED_SLIDES = ("https://docs.google.com/presentation/d/e/2PACX-1vQvvAavICHMLUbWmsf61UA_nmfbL67G0fTT9Ea6D7LON"
                    "YcxQzKsnLhG2leydXOloXljHEtCNfVlAtr5/embed?start=false&loop=false&delayms=3000")
DRIVE_PY = "https://drive.google.com/file/d/1J6lgZWcxPzpCpjujJSnW1aAkCYF6Yxv3/view?usp=drive_link"
DRIVE_FOLDER = "https://drive.google.com/drive/folders/19POzOxjeQ5b5b_8HHAfTb5-mqYqF-Heh"
GOOGLE_DOC = "https://docs.google.com/document/d/1W11sWBigWIsi9wVIhhFx3wajIEnP0Qfb-ei8Ecf5VKU/edit?usp=sharing"


def _one(output_dir: Path, task: DownloadTask, timeout: float = 300):
    with DownloadEngine(output_dir) as engine:
        (task_id,) = engine.submit([task])
        assert engine.wait(timeout=timeout)
        outcome = engine.outcomes()[task_id]
    assert outcome.status == "done", outcome.message
    return outcome, [Path(f) for f in outcome.files]


def test_tab_hidden_syllabus_links_are_found():
    final_url, views = render_interactive(MODERN_SW)
    links = extract_views([(v.label, v.html) for v in views], final_url)
    slides = [link for link in links if identify(link.url).kind == ResourceKind.GOOGLE_SLIDES]
    assert len(slides) >= 10
    assert all(link.context_path.startswith("[Syllabus]") for link in slides)


def test_google_slides_export_pdf_and_pptx(tmp_path):
    _, files = _one(tmp_path, DownloadTask(url=SLIDES, folder=["Week 1"]))
    by_ext = {f.suffix: f for f in files}
    assert by_ext[".pdf"].read_bytes()[:4] == b"%PDF"
    assert zipfile.is_zipfile(by_ext[".pptx"])
    assert by_ext[".pdf"].stem == by_ext[".pptx"].stem == "Lecture_9-29-25_public"


def test_google_doc_export(tmp_path):
    _, (pdf,) = _one(tmp_path, DownloadTask(url=GOOGLE_DOC))
    assert pdf.suffix == ".pdf" and pdf.read_bytes()[:4] == b"%PDF"


def test_published_slides_are_rendered_to_pdf(tmp_path):
    _, (pdf,) = _one(tmp_path, DownloadTask(url=PUBLISHED_SLIDES, name="Course intro"))
    data = pdf.read_bytes()
    assert data[:4] == b"%PDF" and data.count(b"/Type /Page ") >= 3


def test_drive_file_that_triggered_the_virus_scan_page(tmp_path):
    _, (py,) = _one(tmp_path, DownloadTask(url=DRIVE_PY))
    assert py.name == "simple_mcp.py"


def test_public_drive_folder_listing():
    title, entries = list_drive_folder(identify(DRIVE_FOLDER).resource_id)
    assert title and len(entries) > 10
    assert any(e.title.endswith(".pdf") for e in entries)


def test_arxiv_abs_page_downloads_pdf(tmp_path):
    _, (pdf,) = _one(tmp_path, DownloadTask(url="https://arxiv.org/abs/1706.03762", name="Attention"))
    assert pdf.name == "Attention.pdf" and pdf.read_bytes()[:4] == b"%PDF"


def test_github_subfolder_is_rezipped(tmp_path):
    url = "https://github.com/mees-robot-learning-course/ethz-course-2026/tree/main/hw1_pytorch_tutorial"
    _, (archive,) = _one(tmp_path, DownloadTask(url=url))
    assert archive.name == "hw1_pytorch_tutorial.zip"
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
    assert names and not any(n.startswith("hw1_pytorch_tutorial/") for n in names)


def test_github_single_file(tmp_path):
    _, (readme,) = _one(tmp_path, DownloadTask(url="https://github.com/octocat/Hello-World/blob/master/README"))
    assert readme.read_text().startswith("Hello World")


def test_youtube_video_with_subtitles(tmp_path):
    outcome, files = _one(tmp_path, DownloadTask(url="https://www.youtube.com/watch?v=jNQXAC9IVRw", name="zoo"))
    assert any(f.suffix == ".mp4" and f.stat().st_size > 100_000 for f in files)


def test_webpage_saved_as_pdf(tmp_path):
    _, (pdf,) = _one(tmp_path, DownloadTask(url="https://example.com/", kind=ResourceKind.WEBPAGE))
    assert pdf.read_bytes()[:4] == b"%PDF"
