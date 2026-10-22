"""Google Docs / Slides / Sheets exports, Drive files and public Drive folders."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from bs4 import BeautifulSoup

from ..browser import open_page
from ..config import (
    DRIVE_FOLDER_MAX_DEPTH,
    GOOGLE_DOC_FORMATS,
    GOOGLE_SHEET_FORMATS,
    GOOGLE_SLIDES_FORMATS,
    PLAYWRIGHT_TIMEOUT_MS,
    PUBLISHED_SLIDES_MAX,
    REQUEST_TIMEOUT,
)
from ..state import DownloadTask, ErrorKind
from ..state import ResourceKind as K
from .context import HandlerResult, TaskContext
from .errors import Cancelled, DownloadError, error_for_status
from .http import download, looks_like_login, session
from .webpage import clean_title, save_page_pdf

# kind -> (URL path segment, {format: export suffix})
_EXPORTS = {
    K.GOOGLE_DOC: ("document", {
        "pdf": "export?format=pdf", "docx": "export?format=docx", "odt": "export?format=odt",
        "txt": "export?format=txt", "epub": "export?format=epub",
    }),
    K.GOOGLE_SLIDES: ("presentation", {
        "pdf": "export/pdf", "pptx": "export/pptx", "odp": "export/odp", "txt": "export/txt",
    }),
    K.GOOGLE_SHEET: ("spreadsheets", {
        "xlsx": "export?format=xlsx", "pdf": "export?format=pdf", "ods": "export?format=ods",
        "csv": "export?format=csv",
    }),
}
_DEFAULT_FORMATS = {
    K.GOOGLE_DOC: GOOGLE_DOC_FORMATS,
    K.GOOGLE_SLIDES: GOOGLE_SLIDES_FORMATS,
    K.GOOGLE_SHEET: GOOGLE_SHEET_FORMATS,
}
_EXPECT = {"pdf": "pdf", "pptx": "zip", "docx": "zip", "xlsx": "zip", "odt": "zip", "odp": "zip", "ods": "zip", "epub": "zip"}
_QUOTA = re.compile(r"too many users have viewed or downloaded|download quota|quota exceeded", re.IGNORECASE)


def _explain(exc: DownloadError) -> DownloadError:
    if exc.kind == ErrorKind.AUTH_REQUIRED:
        return DownloadError(exc.kind, "not shared publicly (Google asked for a login)", exc.detail)
    if exc.kind == ErrorKind.FORBIDDEN:
        return DownloadError(exc.kind, "access denied: private, or the owner disabled downloads for viewers", exc.detail)
    if exc.kind == ErrorKind.NOT_A_FILE and _QUOTA.search(exc.detail):
        return DownloadError(ErrorKind.RATE_LIMITED, "Google's download quota for this file is exhausted; retry later")
    return exc


# ---------- Docs / Slides / Sheets ----------


def export_document(ctx: TaskContext) -> HandlerResult:
    ref = ctx.ref
    if ref.published:
        return _export_published(ctx)

    segment, exports = _EXPORTS[ref.kind]
    formats = [f.lower().lstrip(".") for f in ctx.task.formats if f.lower().lstrip(".") in exports]
    formats = formats or list(_DEFAULT_FORMATS[ref.kind])

    files: list[Path] = []
    errors: list[tuple[str, DownloadError]] = []
    for fmt in formats:
        url = f"https://docs.google.com/{segment}/d/{ref.resource_id}/{exports[fmt]}"
        try:
            files.append(download(url, ctx.claim, ext=f".{fmt}", expect=_EXPECT.get(fmt),
                                  progress=ctx.progress, cancel=ctx.cancel))
        except Cancelled:
            raise
        except DownloadError as exc:
            errors.append((fmt, _explain(exc)))

    if not files:
        raise errors[0][1]
    message = "; ".join(f"{fmt} failed: {exc}" for fmt, exc in errors)
    return HandlerResult(files, message=message)


def _export_published(ctx: TaskContext) -> HandlerResult:
    pub_id = ctx.ref.resource_id
    if ctx.ref.kind == K.GOOGLE_SHEET:
        url = f"https://docs.google.com/spreadsheets/d/e/{pub_id}/pub?output=xlsx"
        try:
            return HandlerResult([download(url, ctx.claim, ext=".xlsx", expect="zip",
                                           progress=ctx.progress, cancel=ctx.cancel)])
        except DownloadError as exc:
            raise _explain(exc) from exc
    if ctx.ref.kind == K.GOOGLE_DOC:
        return HandlerResult([save_page_pdf(ctx, f"https://docs.google.com/document/d/e/{pub_id}/pub")])
    return HandlerResult([render_published_slides(ctx)])


def render_published_slides(ctx: TaskContext) -> Path:
    """Published decks have no export endpoint: step through the viewer and capture each slide."""
    url = f"https://docs.google.com/presentation/d/e/{ctx.ref.resource_id}/pub?start=false&loop=false"
    width, height = 1600, 900
    frames: list[bytes] = []
    total_bytes = 0
    with open_page(viewport=(width, height)) as page:
        response = page.goto(url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT_MS)
        if response is not None and response.status >= 400:
            raise error_for_status(response.status, url)
        base = ctx.claim(clean_title(page.title()))
        last = None
        for _ in range(PUBLISHED_SLIDES_MAX):
            ctx.check_cancelled()
            shot = page.screenshot(type="jpeg", quality=88)
            digest = hashlib.sha1(shot).digest()
            if digest == last:
                break  # the arrow key no longer changes anything: past the last slide
            frames.append(shot)
            last = digest
            total_bytes += len(shot)
            ctx.progress(total_bytes, None)
            page.keyboard.press("ArrowRight")
            page.wait_for_timeout(700)

    if not frames:
        raise DownloadError(ErrorKind.NOT_A_FILE, "published deck rendered no slides")
    dest = base.with_name(base.name + ".pdf")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    jpegs_to_pdf(frames, part)
    part.replace(dest)
    return dest


def jpeg_size(data: bytes) -> tuple[int, int]:
    """(width, height) from a JPEG's start-of-frame marker."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    raise ValueError("not a JPEG with a baseline or progressive frame")


def jpegs_to_pdf(frames: list[bytes], dest: Path) -> None:
    """Write one full-page JPEG per PDF page, embedding the JPEG bytes unchanged."""
    width, height = jpeg_size(frames[0])
    page_w = 960  # points: 13.33 in, the width of a standard 16:9 slide
    page_h = round(page_w * height / width)
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # placeholders, filled in once the page ids are known
    pages = add(b"")
    page_ids = []
    for i, jpeg in enumerate(frames):
        width, height = jpeg_size(jpeg)
        image = add(
            b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "
            b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n" % (width, height, len(jpeg))
            + jpeg + b"\nendstream"
        )
        draw = b"q %d 0 0 %d 0 0 cm /Im%d Do Q" % (page_w, page_h, i)
        content = add(b"<< /Length %d >>\nstream\n" % len(draw) + draw + b"\nendstream")
        page_ids.append(add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] /Contents %d 0 R "
            b"/Resources << /XObject << /Im%d %d 0 R >> >> >>" % (pages, page_w, page_h, content, i, image)
        ))
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages
    kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
    objects[pages - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids))

    with dest.open("wb") as fh:
        fh.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for number, body in enumerate(objects, start=1):
            offsets.append(fh.tell())
            fh.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
        xref = fh.tell()
        fh.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
        for offset in offsets:
            fh.write(b"%010d 00000 n \n" % offset)
        fh.write(b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, catalog, xref))


# ---------- Drive files ----------


def drive_file(ctx: TaskContext) -> HandlerResult:
    file_id = ctx.ref.resource_id
    # The usercontent endpoint with confirm=t skips the "can't scan for viruses" interstitial
    # that the old uc?export=download endpoint shows for large or executable-looking files.
    url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
    try:
        return HandlerResult([download(url, ctx.claim, progress=ctx.progress, cancel=ctx.cancel)])
    except Cancelled:
        raise
    except DownloadError as exc:
        if ctx.ref.params.get("maybe_folder") and exc.kind in (ErrorKind.NOT_FOUND, ErrorKind.NOT_A_FILE):
            return drive_folder(ctx)
        raise _explain(exc) from exc


# ---------- Drive folders ----------


@dataclass(frozen=True)
class DriveEntry:
    id: str
    url: str
    title: str
    is_folder: bool


def list_drive_folder(folder_id: str) -> tuple[str, list[DriveEntry]]:
    """Title and entries of a public Drive folder, without an API key."""
    url = f"https://drive.google.com/embeddedfolderview?id={folder_id}"
    resp = session().get(url, timeout=REQUEST_TIMEOUT)
    if resp.status_code >= 400:
        raise _explain(error_for_status(resp.status_code, url))
    if looks_like_login(resp.text, str(resp.url)):
        raise DownloadError(ErrorKind.AUTH_REQUIRED, "Drive folder is not shared publicly")
    return parse_folder_view(resp.text)


def parse_folder_view(html: str) -> tuple[str, list[DriveEntry]]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    entries = []
    for div in soup.select("div.flip-entry"):
        link = div.find("a", href=True)
        if link is None:
            continue
        name = div.select_one(".flip-entry-title")
        href = link["href"]
        entries.append(DriveEntry(
            id=(div.get("id") or "").removeprefix("entry-"),
            url=href,
            title=name.get_text(strip=True) if name else "",
            is_folder="/folders/" in href,
        ))
    return title, entries


def drive_folder(ctx: TaskContext) -> HandlerResult:
    """Expand a folder into one child task per entry; the engine schedules them."""
    title, entries = list_drive_folder(ctx.ref.resource_id)
    folder = [*ctx.task.folder, ctx.task.name or title or "Drive folder"]
    depth = ctx.task.depth + 1
    children, skipped = [], 0
    for entry in entries:
        if entry.is_folder and depth > DRIVE_FOLDER_MAX_DEPTH:
            skipped += 1
            continue
        name = entry.title if entry.is_folder else _drop_extension(entry.title)
        children.append(DownloadTask(
            url=entry.url, folder=folder, name=name or None, formats=ctx.task.formats,
            note=ctx.task.note, parent_id=ctx.task.task_id, depth=depth,
        ))
    message = f"folder with {len(entries)} entries"
    if skipped:
        message += f"; {skipped} subfolders deeper than {DRIVE_FOLDER_MAX_DEPTH} levels skipped"
    return HandlerResult([], children=children, message=message)


def _drop_extension(title: str) -> str:
    suffix = PurePosixPath(title).suffix
    return title[: -len(suffix)] if 1 < len(suffix) <= 6 else title
