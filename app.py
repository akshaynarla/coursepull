"""CoursePull app.

    uv run python app.py                     open the app in your browser
    uv run python app.py --native            open it in its own window (needs: uv add pywebview)
    uv run python app.py --replay runs/x/.coursepull/events.jsonl
                                             replay a recorded run, without any LLM calls
"""

from __future__ import annotations

import argparse
from pathlib import Path

from scraper.ui.page import serve

if __name__ in {"__main__", "__mp_main__"}:
    parser = argparse.ArgumentParser(prog="app.py", description="CoursePull: collect an open course for offline study.")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--native", action="store_true", help="open in a desktop window instead of the browser")
    parser.add_argument("--no-show", action="store_true", help="don't open a browser")
    parser.add_argument("--replay", type=Path, help="replay a recorded run (events.jsonl) instead of starting one")
    parser.add_argument("--speed", type=float, default=10.0, help="replay speed-up (default 10x)")
    args = parser.parse_args()
    serve(port=args.port, native=args.native, show=not args.no_show, replay=args.replay, speed=args.speed)
