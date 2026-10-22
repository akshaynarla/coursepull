"""Videos and playlists from any site yt-dlp supports."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ..config import VIDEO_MAX_HEIGHT, VIDEO_SUBTITLE_LANGS, VIDEO_SUBTITLES
from ..state import ErrorKind
from .context import HandlerResult, TaskContext
from .errors import Cancelled, DownloadError

log = logging.getLogger(__name__)

_AUTH = re.compile(r"sign in|log ?in|private video|members.only|registered users|authenticat|passcode|"
                   r"confirm your age|cookies", re.IGNORECASE)
_GONE = re.compile(r"video unavailable|not available|does not exist|has been removed|HTTP Error 404", re.IGNORECASE)
_RATE = re.compile(r"HTTP Error 429|too many requests|rate.limit", re.IGNORECASE)
_SUBS = re.compile(r"subtitle", re.IGNORECASE)


class _Logger:
    """Route yt-dlp output into our log instead of the console."""

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.info("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.warning("yt-dlp: %s", msg)


def _classify(message: str) -> DownloadError:
    if "not a bot" in message:
        return DownloadError(ErrorKind.RATE_LIMITED, "YouTube bot check triggered; retry later")
    if _AUTH.search(message):
        return DownloadError(ErrorKind.AUTH_REQUIRED, "video needs a login or is private")
    if _GONE.search(message):
        return DownloadError(ErrorKind.NOT_FOUND, "video unavailable")
    if _RATE.search(message):
        return DownloadError(ErrorKind.RATE_LIMITED, "video host is rate limiting")
    if "Unsupported URL" in message:
        return DownloadError(ErrorKind.UNSUPPORTED, "no video found at this URL")
    return DownloadError(ErrorKind.UNKNOWN, message[-300:])


def download_video(ctx: TaskContext) -> HandlerResult:
    import yt_dlp
    from yt_dlp.utils import DownloadCancelled
    from yt_dlp.utils import DownloadError as YtDlpError

    playlist = ctx.ref.params.get("playlist") == "1"
    base_opts = {"quiet": True, "no_warnings": True, "noprogress": True, "logger": _Logger(),
                 "noplaylist": not playlist, "socket_timeout": 30}

    try:
        with yt_dlp.YoutubeDL({**base_opts, "extract_flat": "in_playlist"}) as ydl:
            info = ydl.extract_info(ctx.ref.url, download=False)
    except YtDlpError as exc:
        raise _classify(str(exc)) from exc
    ctx.check_cancelled()

    base = ctx.claim(info.get("title"))
    if playlist:
        template = str(base / "%(playlist_index)03d - %(title).80B.%(ext)s")
    else:
        template = str(base) + ".%(ext)s"

    finished_bytes = 0

    def hook(d: dict) -> None:
        nonlocal finished_bytes
        if ctx.cancel.is_set():
            raise DownloadCancelled()
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = finished_bytes + (d.get("downloaded_bytes") or 0)
            ctx.progress(done, finished_bytes + int(total) if total else None)
        elif d["status"] == "finished":
            finished_bytes += d.get("total_bytes") or d.get("downloaded_bytes") or 0

    h = VIDEO_MAX_HEIGHT
    opts = {
        **base_opts,
        # Best mp4 up to the height cap; video and audio are merged by ffmpeg.
        "format": f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}][ext=mp4]/bv*[height<={h}]+ba/b[height<={h}]/b",
        "merge_output_format": "mp4",
        "outtmpl": {"default": template},
        "paths": {"temp": str(ctx.tmp_dir)},
        "progress_hooks": [hook],
        "windowsfilenames": True,
        "retries": 3,
        "fragment_retries": 3,
        "ignoreerrors": "only_download" if playlist else False,
        "writesubtitles": VIDEO_SUBTITLES,
        "writeautomaticsub": VIDEO_SUBTITLES,
        "subtitleslangs": list(VIDEO_SUBTITLE_LANGS),
    }

    def run(options: dict) -> None:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.process_ie_result(info, download=True)

    message = ""
    try:
        try:
            run(opts)
        except YtDlpError as exc:
            if not (VIDEO_SUBTITLES and _SUBS.search(str(exc))):
                raise
            message = "subtitles unavailable"
            run({**opts, "writesubtitles": False, "writeautomaticsub": False})
    except DownloadCancelled as exc:
        raise Cancelled() from exc
    except YtDlpError as exc:
        if ctx.cancel.is_set():
            raise Cancelled() from exc
        raise _classify(str(exc)) from exc

    files = _produced_files(base, playlist)
    if not files:
        raise DownloadError(ErrorKind.UNKNOWN, "yt-dlp finished without writing a file")
    return HandlerResult(files, message=message)


def _produced_files(base: Path, playlist: bool) -> list[Path]:
    unfinished = (".part", ".ytdl", ".temp")
    if playlist:
        if not base.is_dir():
            return []
        return sorted(p for p in base.iterdir() if p.is_file() and not p.name.endswith(unfinished))
    if not base.parent.is_dir():
        return []
    prefix = base.name + "."
    return sorted(
        p for p in base.parent.iterdir()
        if p.is_file() and p.name.startswith(prefix) and not p.name.endswith(unfinished)
    )
