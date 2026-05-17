"""Downloaders for each PlanItem action. All idempotent."""

from __future__ import annotations

import io
import logging
import re
import tempfile
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

import requests

from .config import (
    DOWNLOAD_CHUNK_BYTES,
    DOWNLOAD_CONCURRENCY,
    DOWNLOAD_TIMEOUT,
    USER_AGENT,
)
from .models import DownloadResult, PlanItem

log = logging.getLogger(__name__)

SKIPPED_FILENAME = "skipped.md"
_SKIPPED_LOCK = threading.Lock()


# ---------- helpers ----------


def _dest_path(output_dir: Path, item: PlanItem) -> Path:
    parts: list[str] = []
    if item.chapter:
        parts.append(item.chapter)
    if item.subfolder:
        parts.append(item.subfolder)
    fallback = Path(unquote(urlparse(item.url).path)).name or "download"
    parts.append(item.filename or fallback)
    return output_dir.joinpath(*parts)


def _ensure_parent(dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)


def _stream_to_file(resp: requests.Response, dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    with tmp.open("wb") as fh:
        for chunk in resp.iter_content(DOWNLOAD_CHUNK_BYTES):
            if chunk:
                fh.write(chunk)
    tmp.replace(dest)


def _get_streaming(url: str) -> requests.Response:
    resp = requests.get(
        url,
        stream=True,
        timeout=DOWNLOAD_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        allow_redirects=True,
    )
    resp.raise_for_status()
    return resp


# ---------- PDF ----------


def download_pdf(url: str, dest: Path) -> DownloadResult:
    if dest.exists():
        return DownloadResult(url=url, action="download_pdf", ok=True, path=str(dest), message="exists")
    _ensure_parent(dest)
    with _get_streaming(url) as resp:
        ctype = resp.headers.get("Content-Type", "").lower()
        if not (ctype.startswith("application/pdf") or url.lower().endswith(".pdf")):
            return DownloadResult(
                url=url,
                action="download_pdf",
                ok=False,
                message=f"not a PDF (Content-Type={ctype!r})",
            )
        _stream_to_file(resp, dest)
    return DownloadResult(url=url, action="download_pdf", ok=True, path=str(dest))


# ---------- arxiv ----------

_ARXIV_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf|html)/(?P<id>[\w./-]+?)(?:v\d+)?(?:\.pdf)?$",
    re.IGNORECASE,
)


def download_arxiv(url: str, dest: Path) -> DownloadResult:
    m = _ARXIV_RE.search(url)
    pdf_url = f"https://arxiv.org/pdf/{m.group('id')}.pdf" if m else url
    result = download_pdf(pdf_url, dest)
    return result.model_copy(update={"url": url, "action": "download_arxiv"})


# ---------- GitHub ----------

_GITHUB_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?"
    r"(?:/(?P<kind>tree|blob)/(?P<ref>[^/]+)(?P<subpath>/.*)?)?/?$",
    re.IGNORECASE,
)


def _parse_github_url(url: str) -> tuple[str, str, str | None, str | None]:
    m = _GITHUB_RE.match(url)
    if not m:
        raise ValueError(f"Not a recognized github URL: {url}")
    owner, repo = m.group("owner"), m.group("repo")
    ref = m.group("ref")
    subpath = (m.group("subpath") or "").lstrip("/") or None
    if m.group("kind") == "blob" and subpath:
        parent = str(Path(subpath).parent).replace("\\", "/")
        subpath = parent if parent not in ("", ".") else None
    return owner, repo, ref, subpath


def _default_branch(owner: str, repo: str) -> str:
    resp = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
        timeout=DOWNLOAD_TIMEOUT,
    )
    resp.raise_for_status()
    branch = resp.json().get("default_branch")
    if not branch:
        raise RuntimeError(f"No default_branch for {owner}/{repo}")
    return branch


def _fetch_repo_zip(owner: str, repo: str, ref: str) -> bytes:
    for kind in ("heads", "tags"):
        url = f"https://codeload.github.com/{owner}/{repo}/zip/refs/{kind}/{ref}"
        resp = requests.get(url, timeout=DOWNLOAD_TIMEOUT, headers={"User-Agent": USER_AGENT})
        if resp.status_code == 200:
            return resp.content
    resp.raise_for_status()
    raise RuntimeError(f"Could not fetch {owner}/{repo}@{ref}")


def _extract_subpath_to_zip(zip_bytes: bytes, subpath: str, out_zip: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as src:
        names = src.namelist()
        if not names:
            raise RuntimeError("Empty archive")
        root = names[0].split("/", 1)[0]
        prefix = f"{root}/{subpath.strip('/')}/"
        members = [n for n in names if n.startswith(prefix)]
        if not members:
            raise RuntimeError(f"Subpath {subpath!r} not in archive")
        with tempfile.TemporaryDirectory() as tmp:
            src.extractall(tmp, members=members)
            extracted = Path(tmp) / root / subpath.strip("/")
            with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as out:
                for fp in extracted.rglob("*"):
                    if fp.is_file():
                        out.write(fp, fp.relative_to(extracted))


def download_github_zip(url: str, dest: Path) -> DownloadResult:
    if dest.exists():
        return DownloadResult(url=url, action="download_github_zip", ok=True, path=str(dest), message="exists")
    _ensure_parent(dest)
    owner, repo, ref, subpath = _parse_github_url(url)
    if ref is None:
        ref = _default_branch(owner, repo)
    zip_bytes = _fetch_repo_zip(owner, repo, ref)
    if subpath:
        _extract_subpath_to_zip(zip_bytes, subpath, dest)
    else:
        dest.write_bytes(zip_bytes)
    return DownloadResult(url=url, action="download_github_zip", ok=True, path=str(dest))


# ---------- YouTube ----------


def download_youtube(url: str, dest: Path) -> DownloadResult:
    if dest.parent.exists():
        existing = next(iter(dest.parent.glob(dest.stem + ".*")), None)
        if existing is not None:
            return DownloadResult(url=url, action="download_youtube", ok=True, path=str(existing), message="exists")
    _ensure_parent(dest)

    import yt_dlp

    opts = {
        "outtmpl": str(dest.with_suffix("")) + ".%(ext)s",
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        produced = Path(ydl.prepare_filename(info))
    return DownloadResult(url=url, action="download_youtube", ok=True, path=str(produced))


# ---------- Google Drive / Docs / Slides / Sheets ----------

_GDRIVE_FILE_RE = re.compile(r"drive\.google\.com/file/d/([\w-]+)", re.IGNORECASE)
_GDOC_RE = re.compile(r"docs\.google\.com/document/d/([\w-]+)", re.IGNORECASE)
_GSLIDES_RE = re.compile(r"docs\.google\.com/presentation/d/([\w-]+)", re.IGNORECASE)
_GSHEETS_RE = re.compile(r"docs\.google\.com/spreadsheets/d/([\w-]+)", re.IGNORECASE)


def _gdrive_export_url(url: str) -> str | None:
    if m := _GDOC_RE.search(url):
        return f"https://docs.google.com/document/d/{m.group(1)}/export?format=pdf"
    if m := _GSLIDES_RE.search(url):
        return f"https://docs.google.com/presentation/d/{m.group(1)}/export/pdf"
    if m := _GSHEETS_RE.search(url):
        return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=xlsx"
    if m := _GDRIVE_FILE_RE.search(url):
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    return None


def download_gdrive(url: str, dest: Path) -> DownloadResult:
    export_url = _gdrive_export_url(url)
    if export_url is None:
        return DownloadResult(url=url, action="download_gdrive", ok=False, message="not a recognized Drive URL")
    if dest.exists():
        return DownloadResult(url=url, action="download_gdrive", ok=True, path=str(dest), message="exists")
    _ensure_parent(dest)

    with requests.Session() as sess:
        sess.headers["User-Agent"] = USER_AGENT
        resp = sess.get(export_url, stream=True, timeout=DOWNLOAD_TIMEOUT, allow_redirects=True)
        # Large Drive files redirect to a confirmation HTML page; follow the token.
        if "text/html" in resp.headers.get("Content-Type", "").lower():
            body = resp.text
            confirm = re.search(r'href="(/uc\?export=download[^"]*confirm=[^"]+)"', body)
            if confirm:
                follow = "https://drive.google.com" + confirm.group(1).replace("&amp;", "&")
                resp = sess.get(follow, stream=True, timeout=DOWNLOAD_TIMEOUT, allow_redirects=True)
            else:
                return DownloadResult(
                    url=url,
                    action="download_gdrive",
                    ok=False,
                    message="Drive returned an HTML page with no confirm token (likely a folder or access-restricted)",
                )
        resp.raise_for_status()
        _stream_to_file(resp, dest)
    return DownloadResult(url=url, action="download_gdrive", ok=True, path=str(dest))


# ---------- side notes ----------


def note_paywalled(url: str, reason: str, output_dir: Path) -> DownloadResult:
    skipped = output_dir / SKIPPED_FILENAME
    line = f"- [{url}]({url}) — {reason}\n"
    with _SKIPPED_LOCK:
        output_dir.mkdir(parents=True, exist_ok=True)
        existing = skipped.read_text(encoding="utf-8") if skipped.exists() else ""
        if line not in existing:
            with skipped.open("a", encoding="utf-8") as fh:
                fh.write(line)
    return DownloadResult(url=url, action="note_paywalled", ok=True, path=str(skipped), message=reason)


def skip(url: str, reason: str) -> DownloadResult:
    return DownloadResult(url=url, action="skip", ok=True, message=reason)


# ---------- dispatcher + parallel runner ----------


def _execute_one(item: PlanItem, output_dir: Path) -> DownloadResult:
    if item.action == "note_paywalled":
        return note_paywalled(item.url, item.reason, output_dir)
    if item.action == "skip":
        return skip(item.url, item.reason)

    dest = _dest_path(output_dir, item)

    try:
        if item.action == "download_pdf":
            return download_pdf(item.url, dest)
        if item.action == "download_arxiv":
            return download_arxiv(item.url, dest)
        if item.action == "download_github_zip":
            return download_github_zip(item.url, dest)
        if item.action == "download_youtube":
            if not dest.suffix:
                dest = dest.with_suffix(".mp4")
            return download_youtube(item.url, dest)
        if item.action == "download_gdrive":
            return download_gdrive(item.url, dest)
    except Exception as exc:  # noqa: BLE001 — one failure must not kill the run
        log.exception("download failed for %s", item.url)
        return DownloadResult(
            url=item.url, action=item.action, ok=False,
            message=f"{type(exc).__name__}: {exc}",
        )

    return DownloadResult(
        url=item.url, action=item.action, ok=False, message=f"unknown action {item.action!r}"
    )


ProgressFn = Callable[[int, int, PlanItem, DownloadResult], None]


def execute_many(
    items: list[PlanItem],
    output_dir: Path,
    max_workers: int = DOWNLOAD_CONCURRENCY,
    on_done: ProgressFn = lambda *_: None,
) -> list[DownloadResult]:
    if not items:
        return []

    results: list[DownloadResult | None] = [None] * len(items)
    completed = 0
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_idx = {pool.submit(_execute_one, item, output_dir): i for i, item in enumerate(items)}
        for fut in as_completed(future_to_idx):
            idx = future_to_idx[fut]
            result = fut.result()
            results[idx] = result
            with lock:
                completed += 1
                current = completed
            on_done(current, len(items), items[idx], result)

    return [r for r in results if r is not None]
