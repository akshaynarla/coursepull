from __future__ import annotations

import io
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pypdf import PdfWriter


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--run-network", action="store_true", help="also run tests that hit real websites")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-network"):
        return
    skip = pytest.mark.skip(reason="hits real websites; pass --run-network")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


PDF_BYTES = b"%PDF-1.4\n" + b"0" * 4000 + b"\n%%EOF\n"
LOGIN_HTML = b"<!DOCTYPE html><html><head><title>Sign in</title></head><body><form><input type='password'></form></body></html>"
SLOW_CHUNK = b"x" * 64 * 1024
SLOW_CHUNKS = 200


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # keep pytest output clean
        pass

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path.endswith(".pdf") and "missing" not in path:
            self._send(200, PDF_BYTES, "application/pdf")
        elif path == "/notes":
            self._send(200, PDF_BYTES, "application/octet-stream",
                       {"Content-Disposition": "attachment; filename=\"x.pdf\"; filename*=UTF-8''Week%201%3A%20Notes.pdf"})
        elif path == "/login":
            self._send(200, LOGIN_HTML, "text/html; charset=utf-8")
        elif path == "/slow.bin":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(SLOW_CHUNK) * SLOW_CHUNKS))
            self.end_headers()
            try:
                for _ in range(SLOW_CHUNKS):
                    self.wfile.write(SLOW_CHUNK)
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
        else:
            self._send(404, b"not found", "text/plain")


@pytest.fixture(scope="session")
def file_server():
    """Base URL of a local HTTP server serving the fixtures above."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


# ---------- a small course site for end-to-end runs with scripted agents ----------

FILLER = "<p>" + "This course teaches practical software testing. " * 6 + "</p>" + '<a href="#top">top</a>' * 5


def _pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


SITE = {
    "/course/": f"""<html><head><title>Testing 101</title></head><body><h1>Testing 101</h1>
        <nav><a href="/course/readings.html">Readings</a> <a href="https://twitter.com/prof">Twitter</a></nav>
        <h2>Schedule</h2><table>
        <tr><td>Week 1</td><td>Intro</td><td><a href="/course/l1.pdf">Week 1 slides</a></td></tr>
        <tr><td>Week 2</td><td>Mocks</td><td><a href="/course/l2.pdf">Week 2 slides</a></td></tr>
        </table>{FILLER}</body></html>""",
    "/course/readings.html": f"""<html><head><title>Readings</title></head><body><h2>Required reading</h2>
        <ul><li><a href="/course/paper-a.pdf">Paper A</a></li>
        <li><a href="/course/paper-b.pdf">Paper B</a> (<a href="/course/mirror/paper-b.pdf">Paper B mirror</a>)</li>
        </ul>{FILLER}</body></html>""",
}
PDFS = {"/course/l1.pdf", "/course/l2.pdf", "/course/paper-a.pdf", "/course/mirror/paper-b.pdf"}


class _Site(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path in SITE:
            body, ctype = SITE[path].encode(), "text/html; charset=utf-8"
        elif path in PDFS:
            body, ctype = _pdf(), "application/pdf"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="session")
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
