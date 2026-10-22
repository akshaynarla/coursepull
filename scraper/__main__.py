"""Command line: python -m scraper <course-url> <output-dir> [--fresh] [--budget N]

Ctrl+C stops gracefully (the finish step still writes the index); run the same
command again to resume an interrupted run.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import signal
import sys
import threading

import openai

from .config import AGENT_ROLES, LLM_ENDPOINTS, RUN_TOKEN_BUDGET
from .graph import run_course
from .llm import LLMRouter, ollama_running


def _printer(event: dict) -> None:
    kind = event.get("type")
    if kind == "narration":
        print(f"  [{event['agent']}] {event['text']}")
    elif kind == "phase":
        extra = f" ({event['items']} items)" if "items" in event else ""
        print(f"\n== {event['phase']}{extra} ==")
    elif kind == "tool":
        print(f"  [{event['agent']}] -> {event['tool']}{'' if event['ok'] else ' (failed, retrying)'}")
    elif kind == "page":
        print(f"  [{event['agent']}] read {event['url']} ({event['links']} links"
              + (", tabs revealed" if event.get("revealed") else "") + ")")
    elif kind == "plan":
        print(f"  [Tutor] plan: {event['planned']} planned, {event['excluded']} excluded, {event['modules']} modules")
    elif kind == "review":
        print(f"  [Reviewer] {'approved' if event['approved'] else 'changes requested'}: {event['summary']}")
    elif kind == "download" and event["event"] == "finished":
        mark = {"done": "ok ", "failed": "ERR", "cancelled": "-- "}.get(event.get("status"), "?  ")
        detail = f"  ({event.get('error')}: {event.get('message')})" if event.get("status") == "failed" else ""
        print(f"  [{event['done']}/{event['total']}] {mark} {event.get('name') or event['url']}{detail}")
    elif kind == "examination":
        print(f"  [Examiner] {'passed' if event['passed'] else 'not passed'}: {event['summary']}")
    elif kind == "done":
        print(f"\nDone: {event['files']} files from {event['planned']} planned items"
              f", {event['failed']} failed. Index: {event['readme']}")
    elif kind == "run":
        print(("Resuming" if event["resuming"] else "Starting") + f" {event['course_url']} -> {event['output_dir']}")
        print("Models: " + ", ".join(f"{role}={model}" for role, model in event["models"].items()))
    elif kind == "usage":
        print(f"Tokens used: {event['total']:,} ({', '.join(f'{r} {n:,}' for r, n in event['by_role'].items())})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scraper", description="Collect an open course's materials.")
    parser.add_argument("course_url")
    parser.add_argument("output_dir")
    parser.add_argument("--fresh", action="store_true", help="ignore any interrupted run and start over")
    parser.add_argument("--budget", type=int, default=RUN_TOKEN_BUDGET, help="token budget for all agents")
    parser.add_argument("--plan-only", action="store_true", help="stop after the reviewed plan; download nothing")
    parser.add_argument("--model", action="append", default=[], metavar="ROLE=ENDPOINT:MODEL",
                        help="e.g. scout=ollama:qwen3.5:4b, or all=hf:Qwen/Qwen3.6-35B-A3B (repeatable)")
    parser.add_argument("--ask-token", action="store_true",
                        help="prompt for a HuggingFace token (hidden input, used for this run only, never saved)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)  # live output even when redirected to a log file
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        models = _parse_models(args.model)
    except ValueError as exc:
        parser.error(str(exc))

    cancel = threading.Event()

    def stop(_signum, _frame) -> None:
        if cancel.is_set():
            raise KeyboardInterrupt
        print("\nStopping after the current steps (Ctrl+C again to abort)...")
        cancel.set()

    api_keys = {"hf": getpass.getpass("HuggingFace token (input hidden): ").strip()} if args.ask_token else None
    signal.signal(signal.SIGINT, stop)
    try:
        llm = LLMRouter.from_config(api_keys=api_keys, models=models)
        if down := [url for url in llm.ollama_urls() if not ollama_running(url)]:
            print(f"Ollama is not running at {down[0]}. Start the Ollama app (or run `ollama serve`), then try again.")
            return 2
        run_course(args.course_url, args.output_dir, llm=llm, on_event=_printer, cancel=cancel,
                   token_budget=args.budget, fresh=args.fresh, plan_only=args.plan_only)
    except KeyboardInterrupt:
        print("Aborted. Run the same command again to resume.")
        return 130
    except openai.AuthenticationError as exc:
        print(f"\nThe LLM endpoint rejected the API key ({exc.status_code}). For HuggingFace, create a token "
              "with the 'Make calls to Inference Providers' permission and set HF_TOKEN. "
              "Run the same command again to resume.")
        return 2
    except ValueError as exc:  # e.g. no API key configured
        print(f"\n{exc}")
        return 2
    return 0


def _parse_models(specs: list[str]) -> dict[str, dict]:
    models: dict[str, dict] = {}
    for spec in specs:
        role, _, target = spec.partition("=")
        endpoint, _, model = target.partition(":")
        if not (role and endpoint in LLM_ENDPOINTS and model):
            raise ValueError(f"--model {spec!r}: expected ROLE=ENDPOINT:MODEL with ENDPOINT in {list(LLM_ENDPOINTS)}")
        for r in AGENT_ROLES if role == "all" else [role]:
            if r not in AGENT_ROLES:
                raise ValueError(f"--model {spec!r}: unknown role {r!r}; roles are {list(AGENT_ROLES)} or 'all'")
            models[r] = {"endpoint": endpoint, "model": model}
    return models


if __name__ == "__main__":
    sys.exit(main())
