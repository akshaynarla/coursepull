"""The finish step: a course index (README.md) and a list of what was not downloaded."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from ..run_context import RunContext, emit
from ..state import CourseState, Plan, PlanEntry, TaskOutcome

_STOP_REASONS = {"cancelled": "stopped by the user", "budget": "the token budget was used up",
                 "plan_only": "plan-only run, nothing downloaded"}


def _files(entry: PlanEntry, outcomes: dict[str, TaskOutcome]) -> list[str]:
    outcome = outcomes.get(entry.task.task_id)
    if outcome is None:
        return []
    files = list(outcome.files)
    for child_id in outcome.children:
        if (child := outcomes.get(child_id)) and child.status == "done":
            files += child.files
    return files


def _link(path: str, root: Path) -> str:
    rel = os.path.relpath(path, root).replace("\\", "/")
    return f"[{Path(path).suffix.lstrip('.') or Path(path).name}](<{rel}>)"


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def write_report(state: CourseState, rc: RunContext) -> dict:
    root = rc.output_dir
    root.mkdir(parents=True, exist_ok=True)
    plan: Plan = state.get("plan") or Plan()
    outcomes = state.get("outcomes") or {}
    course_map = state.get("course_map")
    brief = state.get("brief")
    exam = state.get("examination")
    stop = state.get("stop_reason") or ("plan_only" if state.get("plan_only") else None)

    title = (course_map.title if course_map else None) or (brief.course_title if brief else state.get("course_url", "Course"))
    all_files = [f for e in plan.entries for f in _files(e, outcomes)]
    total_bytes = sum(Path(f).stat().st_size for f in all_files if Path(f).is_file())
    failed = [e for e in plan.entries if (o := outcomes.get(e.task.task_id)) and o.status != "done"]

    lines = [f"# {title}", ""]
    summary = (course_map.summary if course_map else "") or (brief.summary if brief else "")
    if summary:
        lines += [summary, ""]
    lines += [
        f"- **Source:** {state.get('course_url', '')}",
        f"- **Collected:** {datetime.now():%Y-%m-%d %H:%M}",
        f"- **Planned items:** {len(plan.entries)} · **files:** {len(all_files)} ({_size(total_bytes)})"
        + (f" · **failed:** {len(failed)} (see [skipped.md](skipped.md))" if failed else ""),
    ]
    if brief and brief.term:
        lines.append(f"- **Term:** {brief.term} — the most complete offering on the site ({brief.term_reason})")
    if stop:
        lines.append(f"- **Stopped early:** {_STOP_REASONS.get(stop, stop)}")
    if exam:
        lines.append(f"- **Examiner:** {'passed' if exam.passed else 'not passed'} — {exam.summary}")
    lines.append("")

    modules = list(course_map.modules) if course_map else []
    groups: dict[str | None, list[PlanEntry]] = {}
    for entry in plan.entries:
        groups.setdefault(entry.module_key if any(m.key == entry.module_key for m in modules) else None,
                          []).append(entry)
    sections = [(f"{m.key} · {m.title}", groups.get(m.key, [])) for m in modules]
    sections.append(("Course-wide", groups.get(None, [])))
    for heading, entries in sections:
        if not entries:
            continue
        lines += [f"## {heading}", ""]
        for entry in entries:
            files = _files(entry, outcomes)
            label = f"**{entry.task.name or entry.material_id}** ({entry.role.replace('_', ' ')}"
            label += ", optional)" if not entry.required else ")"
            outcome = outcomes.get(entry.task.task_id)
            if files:
                links = " · ".join(_link(f, root) for f in files[:12])
                more = f" · +{len(files) - 12} more" if len(files) > 12 else ""
                note = outcome.message if outcome and outcome.message != "already downloaded" else ""
                lines.append(f"- {label} — {links}{more}" + (f" ({note})" if note else ""))
            elif outcome is None:
                lines.append(f"- {label} — not downloaded ({_STOP_REASONS.get(stop, 'not run') if stop else 'not run'})")
            else:
                lines.append(f"- {label} — ✗ {outcome.message or outcome.status}")
        lines.append("")
    (root / "README.md").write_text("\n".join(lines), encoding="utf-8")

    materials = state.get("materials") or {}
    skipped = ["# Not downloaded", ""]
    if failed:
        skipped += ["## Failed downloads", ""]
        for entry in failed:
            o = outcomes[entry.task.task_id]
            kind = o.error_kind.value if o.error_kind else o.status
            skipped.append(f"- **{entry.task.name}** — {kind}: {o.message} — <{entry.task.url}>")
        skipped.append("")
    if plan.excluded:
        skipped += ["## Left out by the Tutor", ""]
        for x in plan.excluded:
            name = materials[x.material_id].title if x.material_id in materials else x.material_id
            skipped.append(f"- {name} — {x.reason} — <{x.url}>")
        skipped.append("")
    (root / "skipped.md").write_text("\n".join(skipped), encoding="utf-8")

    emit({"type": "done", "title": title, "planned": len(plan.entries), "files": len(all_files),
          "bytes": total_bytes, "failed": len(failed), "stop_reason": stop,
          "passed": exam.passed if exam else None, "readme": str(root / "README.md")})
    return {"phase": "finished"}
