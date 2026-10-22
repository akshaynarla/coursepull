"""Streaming HTTP downloads with progress, cancellation and content checks."""

from __future__ import annotations

import mimetypes
import re
import threading
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..config import DOWNLOAD_CHUNK_BYTES, DOWNLOAD_TIMEOUT, MAX_FILE_BYTES, REQUEST_TIMEOUT, USER_AGENT
from ..state import ErrorKind
from .errors import Cancelled, DownloadError, error_for_status

ProgressFn = Callable[[int, "int | None"], None]
ClaimFn = Callable[["str | None"], Path]  # server-suggested stem -> destination base path

_local = threading.local()


def session() -> requests.Session:
    """One Session per thread (requests.Session is not thread-safe)."""
    sess = getattr(_local, "session", None)
    if sess is None:
        sess = requests.Session()
        sess.headers["User-Agent"] = USER_AGENT
        retry = Retry(total=3, backoff_factor=1.0, status_forcelist=(500, 502, 503, 504),
                      allowed_methods=frozenset({"GET", "HEAD"}), raise_on_status=False)
        adapter = HTTPAdapter(max_retries=retry)
        sess.mount("http://", adapter)
        sess.mount("https://", adapter)
        _local.session = sess
    return sess


# ---------- response inspection ----------

_CD_EXT = re.compile(r"filename\*\s*=\s*([\w-]+)'[^']*'([^;]+)", re.IGNORECASE)
_CD_PLAIN = re.compile(r'filename\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^;]+))', re.IGNORECASE)


def parse_content_disposition(value: str | None) -> str | None:
    """Filename from a Content-Disposition header; RFC 5987 filename* wins."""
    # Titles like "Lecture 9/29/25" arrive with slashes; keep them as dashes rather than
    # treating them as directories. The path allocator sanitises the rest.
    if not value:
        return None
    if m := _CD_EXT.search(value):
        encoding = m.group(1) or "utf-8"
        try:
            name = unquote(m.group(2).strip().strip('"'), encoding=encoding)
            return re.sub(r"[/\\]", "-", name).strip() or None
        except LookupError:
            pass
    if m := _CD_PLAIN.search(value):
        name = (m.group(1) if m.group(1) is not None else m.group(2) or "").strip()
        return re.sub(r"[/\\]", "-", name).strip() or None
    return None


def sniff(head: bytes) -> str:
    """Rough file type from the first bytes."""
    start = head[:512].lstrip().lower()
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(b"\x1f\x8b"):
        return "gzip"
    if start.startswith((b"<!doctype html", b"<html", b"<head", b"<!--")) or b"<html" in start[:200]:
        return "html"
    if head[4:8] == b"ftyp":
        return "mp4"
    if head.startswith(b"\x89PNG"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    return "unknown"


_SNIFF_EXT = {"pdf": ".pdf", "zip": ".zip", "gzip": ".gz", "mp4": ".mp4", "png": ".png", "jpeg": ".jpg", "html": ".html"}
_LOGIN_URL = re.compile(
    r"accounts\.google\.com|login\.microsoftonline|/login|/signin|/sign_in|/sso/|/cas/login|"
    r"shibboleth|/idp/|/saml|/oauth|/auth/realms",
    re.IGNORECASE,
)
_LOGIN_TITLE = re.compile(r"<title>[^<]*(sign in|log ?in|anmelden|connexion|authenticat)", re.IGNORECASE)
_TITLE = re.compile(r"<title[^>]*>([^<]{0,200})", re.IGNORECASE)


def looks_like_login(html: str, final_url: str) -> bool:
    return bool(_LOGIN_URL.search(final_url) or _LOGIN_TITLE.search(html) or 'type="password"' in html.lower())


def html_title(html: str) -> str:
    m = _TITLE.search(html)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def _split_name(name: str | None) -> tuple[str | None, str | None]:
    if not name:
        return None, None
    p = PurePosixPath(name)
    ext = p.suffix.lower() if 1 < len(p.suffix) <= 6 else None
    return (p.stem if ext else p.name) or None, ext


def _url_name(url: str) -> str | None:
    name = PurePosixPath(unquote(urlparse(url).path)).name
    return name or None


def _ext_for_type(content_type: str) -> str | None:
    mime = content_type.split(";")[0].strip().lower()
    if not mime or mime in ("application/octet-stream", "binary/octet-stream"):
        return None
    return {"text/plain": ".txt", "application/x-ipynb+json": ".ipynb"}.get(mime) or mimetypes.guess_extension(mime)


# ---------- download ----------


def download(
    url: str,
    claim: ClaimFn,
    *,
    progress: ProgressFn,
    cancel: threading.Event,
    ext: str | None = None,
    expect: str | None = None,
    max_bytes: int = MAX_FILE_BYTES,
    headers: dict[str, str] | None = None,
) -> Path:
    """GET a URL and stream it to disk.

    `claim` turns the server-suggested name into a destination base path; the
    extension is `ext`, else the server's, else derived from the content type.
    `expect` ("pdf", "zip", ...) rejects anything that does not sniff as that.
    HTML where a file was expected becomes AUTH_REQUIRED (login page) or NOT_A_FILE.
    """
    if cancel.is_set():
        raise Cancelled()
    try:
        resp = session().get(url, stream=True, timeout=(REQUEST_TIMEOUT, DOWNLOAD_TIMEOUT),
                             allow_redirects=True, headers=headers)
    except requests.RequestException as exc:
        raise DownloadError(ErrorKind.NETWORK, f"{type(exc).__name__}: {exc}") from exc

    with resp:
        if resp.status_code >= 300:  # redirects are followed; anything else 3xx is not a file
            raise error_for_status(resp.status_code, url)
        chunks = resp.iter_content(DOWNLOAD_CHUNK_BYTES)
        try:
            first = next(chunks, b"")
        except requests.RequestException as exc:
            raise DownloadError(ErrorKind.NETWORK, f"{type(exc).__name__}: {exc}") from exc
        kind = sniff(first)
        content_type = resp.headers.get("Content-Type", "")

        if kind == "html" and expect != "html":
            page = first
            for chunk in chunks:
                page += chunk
                if len(page) > 256 * 1024:
                    break
            text = page.decode("utf-8", errors="replace")
            if looks_like_login(text, str(resp.url)):
                raise DownloadError(ErrorKind.AUTH_REQUIRED, "redirected to a login page", detail=text[:4000])
            raise DownloadError(ErrorKind.NOT_A_FILE, f"got an HTML page ({html_title(text) or 'untitled'})",
                                detail=text[:4000])
        if expect and kind != expect:
            raise DownloadError(ErrorKind.NOT_A_FILE, f"expected {expect}, got {kind} ({content_type or 'no type'})")

        total = int(resp.headers["Content-Length"]) if resp.headers.get("Content-Length", "").isdigit() else None
        if total and total > max_bytes:
            raise DownloadError(ErrorKind.TOO_LARGE, f"{total / 1e9:.1f} GB exceeds the {max_bytes / 1e9:.1f} GB limit")

        server_name = parse_content_disposition(resp.headers.get("Content-Disposition")) or _url_name(str(resp.url))
        server_stem, server_ext = _split_name(server_name)
        ext = ext or server_ext or _ext_for_type(content_type) or _SNIFF_EXT.get(kind) or ".bin"
        base = claim(server_stem)
        dest = base.with_name(base.name + ext)
        dest.parent.mkdir(parents=True, exist_ok=True)

        part = dest.with_name(dest.name + ".part")
        done = 0
        try:
            with part.open("wb") as fh:
                for chunk in _chain(first, chunks):
                    if cancel.is_set():
                        raise Cancelled()
                    if not chunk:
                        continue
                    fh.write(chunk)
                    done += len(chunk)
                    if done > max_bytes:
                        raise DownloadError(ErrorKind.TOO_LARGE, f"exceeded the {max_bytes / 1e9:.1f} GB limit")
                    progress(done, total)
            if done == 0:
                raise DownloadError(ErrorKind.NOT_A_FILE, "server returned an empty file")
        except requests.RequestException as exc:
            part.unlink(missing_ok=True)
            raise DownloadError(ErrorKind.NETWORK, f"{type(exc).__name__}: {exc}") from exc
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        part.replace(dest)
        return dest


def _chain(first: bytes, rest):
    yield first
    yield from rest
