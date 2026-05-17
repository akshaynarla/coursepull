"""Three-agent pipeline: Discoverer → Classifier → downloads → Verifier → retries."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

from .agents.classifier import run_classifier
from .agents.discoverer import run_discoverer
from .agents.runtime import AgentRunner
from .agents.verifier import run_verifier
from .config import DOWNLOAD_CONCURRENCY
from .download import ProgressFn, execute_many
from .models import DownloadResult, PlanItem

log = logging.getLogger(__name__)

LogFn = Callable[[str], None]


def _noop_log(_msg: str) -> None:
    pass


def _noop_progress(*_args: object) -> None:
    pass


def run(
    url: str,
    output_dir: Path,
    hf_token: str,
    log_callback: LogFn = _noop_log,
    progress_callback: ProgressFn = _noop_progress,
) -> list[DownloadResult]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    runner = AgentRunner(api_key=hf_token)

    log_callback("=== Discoverer ===")
    pages = run_discoverer(url, runner, log_callback)

    log_callback("=== Classifier ===")
    plan = run_classifier(url, pages, runner, log_callback)

    log_callback(f"=== Downloads ({len(plan)} items, x{DOWNLOAD_CONCURRENCY}) ===")
    results = execute_many(plan, output_dir, DOWNLOAD_CONCURRENCY, progress_callback)

    failures = [(item, res) for item, res in zip(plan, results) if not res.ok]
    if failures:
        log_callback(f"=== Verifier ({len(failures)} failures) ===")
        revised = run_verifier(failures, runner, log_callback)
        if revised:
            log_callback(f"=== Re-downloads ({len(revised)} items) ===")
            retry_results = execute_many(revised, output_dir, DOWNLOAD_CONCURRENCY, progress_callback)
            results = _merge_retries(plan, results, revised, retry_results)

    _write_readme(output_dir, url, pages, results)
    ok = sum(1 for r in results if r.ok)
    log_callback(f"Done. {ok}/{len(results)} succeeded.")
    return results


def _merge_retries(
    original_plan: list[PlanItem],
    original_results: list[DownloadResult],
    revised: list[PlanItem],
    retry_results: list[DownloadResult],
) -> list[DownloadResult]:
    """Replace failed results with successful retries (keyed by URL)."""
    retry_by_url = {item.url: res for item, res in zip(revised, retry_results)}
    merged: list[DownloadResult] = []
    for item, original in zip(original_plan, original_results):
        retry = retry_by_url.get(item.url)
        if retry is not None and retry.ok:
            merged.append(retry)
        else:
            merged.append(original)
    return merged


def _write_readme(
    output_dir: Path,
    course_url: str,
    pages: dict,
    results: list[DownloadResult],
) -> None:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.action] = counts.get(r.action, 0) + 1
    ok = sum(1 for r in results if r.ok)

    lines = [
        "# Course Resources",
        "",
        f"- **Course URL:** {course_url}",
        f"- **Run at:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **Pages crawled:** {len(pages)}",
        f"- **Plan items:** {len(results)}",
        f"- **Succeeded:** {ok}",
        f"- **Failed:** {len(results) - ok}",
        "",
        "## Pages crawled",
        "",
        *[f"- {p}" for p in pages],
        "",
        "## Counts by action",
        "",
        *[f"- {action}: {counts[action]}" for action in sorted(counts)],
        "",
    ]
    (output_dir / "README.md").write_text("\n".join(lines), encoding="utf-8")
