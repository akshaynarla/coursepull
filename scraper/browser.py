"""Headless Edge via Playwright. One short-lived browser per use, so it is safe
to call from any worker thread.

On Windows, Playwright's own browser.close() waits ~20 s for Edge to exit. We
start Edge ourselves, attach over CDP, and end it by killing its process tree,
which takes well under a second.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import USER_AGENT

log = logging.getLogger(__name__)

_EDGE_CANDIDATES = (
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
)
_STARTUP_TIMEOUT_S = 20


def _edge_executable() -> str | None:
    if sys.platform != "win32":
        return None
    for candidate in _EDGE_CANDIDATES:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("msedge")


def _context_options(viewport: tuple[int, int] | None, locale: str) -> dict:
    options = {
        "user_agent": USER_AGENT,
        "locale": locale,
        "accept_downloads": False,  # clicking around must never start a download
    }
    if viewport:
        options["viewport"] = {"width": viewport[0], "height": viewport[1]}
    return options


@contextmanager
def open_page(viewport: tuple[int, int] | None = None, locale: str = "en-US") -> Iterator:
    from playwright.sync_api import sync_playwright

    edge = _edge_executable()
    with sync_playwright() as p:
        if edge is None:
            browser = p.chromium.launch(channel="msedge", headless=True)
            try:
                yield browser.new_context(**_context_options(viewport, locale)).new_page()
            finally:
                browser.close()
            return

        with _edge_process(edge, locale) as port:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.new_context(**_context_options(viewport, locale))
            try:
                yield context.new_page()
            finally:
                context.close()
                browser.close()  # only disconnects; _edge_process ends Edge itself


@contextmanager
def _edge_process(edge: str, locale: str) -> Iterator[str]:
    profile = Path(tempfile.mkdtemp(prefix="coursepull-edge-"))
    proc = subprocess.Popen(
        [edge, "--headless", "--remote-debugging-port=0", f"--user-data-dir={profile}", f"--lang={locale}",
         "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        yield _wait_for_port(proc, profile / "DevToolsActivePort")
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log.warning("Edge (pid %s) did not exit", proc.pid)
        shutil.rmtree(profile, ignore_errors=True)


def _wait_for_port(proc: subprocess.Popen, port_file: Path) -> str:
    deadline = time.monotonic() + _STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Edge exited during startup (code {proc.returncode})")
        try:
            lines = port_file.read_text().split()
        except OSError:
            lines = []
        if lines:
            return lines[0]
        time.sleep(0.05)
    raise RuntimeError("Edge did not open its debugging port in time")
