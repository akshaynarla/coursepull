"""Regressions from run 9, the full download of the ETH Robot Learning course (Spring 2026)."""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfWriter

from scraper.agents.toolbox import inspect_file
from scraper.extract import extract


def _pdf(path: Path, user_password: str | None) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    if user_password is not None:
        writer.encrypt(user_password=user_password, owner_password="owner", algorithm="AES-256")
    with path.open("wb") as fh:
        writer.write(fh)
    return path


def test_a_password_protected_pdf_is_reported_as_such(tmp_path):
    info = inspect_file(_pdf(tmp_path / "slides.pdf", "course-secret"))
    assert info["error"].startswith("password-protected")


def test_an_aes_pdf_with_only_owner_restrictions_still_opens(tmp_path):
    info = inspect_file(_pdf(tmp_path / "paper.pdf", ""))
    assert "error" not in info and info["pages"] == 1


def test_a_link_in_a_long_table_row_keeps_its_column():
    papers = ", ".join(f'<a href="https://arxiv.org/abs/2001.0000{i}">A long paper title number {i}</a>'
                       for i in range(6))
    html = f"""<table>
      <thead><tr><th>Week</th><th>Monday</th><th>Paper Discussion</th><th>Guest Spotlight</th></tr></thead>
      <tbody><tr>
        <td>Week 2</td>
        <td>Control <a href="https://youtu.be/lecture">Recording</a></td>
        <td>{papers}</td>
        <td><a href="https://example.edu/~guest/">A Guest</a> (Prof.) <a href="https://youtu.be/guest">YouTube Recording</a></td>
      </tr></tbody></table>"""
    snippets = {link.url: link.snippet for link in extract(html, "https://course.example/")}
    assert snippets["https://youtu.be/guest"].startswith("Guest Spotlight: A Guest (Prof.) YouTube Recording")
    assert snippets["https://youtu.be/lecture"].startswith("Monday: Control Recording | row: Week 2")
