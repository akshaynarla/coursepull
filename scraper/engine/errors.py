"""Typed download failures. The ErrorKind is what agents reason about."""

from __future__ import annotations

from ..state import ErrorKind


class DownloadError(Exception):
    def __init__(self, kind: ErrorKind, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.detail = detail  # e.g. the start of an unexpected HTML page


class Cancelled(DownloadError):
    def __init__(self) -> None:
        super().__init__(ErrorKind.CANCELLED, "cancelled")


def error_for_status(status: int, url: str) -> DownloadError:
    if status in (401, 407):
        return DownloadError(ErrorKind.AUTH_REQUIRED, f"HTTP {status}: login required")
    if status == 403:
        return DownloadError(ErrorKind.FORBIDDEN, "HTTP 403: access denied (private, or downloads disabled by the owner)")
    if status in (404, 410):
        return DownloadError(ErrorKind.NOT_FOUND, f"HTTP {status}: not found")
    if status == 429:
        return DownloadError(ErrorKind.RATE_LIMITED, "HTTP 429: rate limited")
    if 300 <= status < 400:
        return DownloadError(ErrorKind.NOT_A_FILE, f"HTTP {status}: no file at this address (ambiguous or unresolved)")
    if status >= 500:
        return DownloadError(ErrorKind.NETWORK, f"HTTP {status}: server error")
    return DownloadError(ErrorKind.UNKNOWN, f"HTTP {status} for {url}")
