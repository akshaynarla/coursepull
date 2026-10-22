from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


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
