"""Eval set: plan frozen course sites and score each plan against a hand-made answer key.

    python -m scraper.evals list
    python -m scraper.evals run robot-learning [more names | all] --ask-token
    python -m scraper.evals score robot-learning downloads/robot-learning

Each course in evals/<name>/ has
  pages.json.gz  the course site as the agents read it (any run's .coursepull/pages.json.gz)
  truth.toml     the answer key: the items a complete plan holds, their module and role

A run reads only the saved pages, so a site that changes (a new term, a dead link)
never moves the score; only the agents and the code do. Runs are plan-only.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import RUN_TOKEN_BUDGET, STATE_DIRNAME
from .identify import identify

EVALS_DIR = Path(__file__).resolve().parent.parent / "evals"
RUNS_DIR = Path("runs") / "eval"


@dataclass
class Expected:
    url: str
    name: str = ""
    module: str | None = None  # "" for course-wide; None: not scored
    role: str | list[str] | None = None  # a list: any of these is right; None: not scored
    optional: bool = False  # fine to plan, not needed: never missing, never an extra


@dataclass
class Truth:
    name: str
    course_url: str
    term: str = ""  # the offering a plan must collect; "" when the site has one
    items: list[Expected] = field(default_factory=list)


def load_truth(name: str, evals_dir: Path = EVALS_DIR) -> Truth:
    data = tomllib.loads((evals_dir / name / "truth.toml").read_text(encoding="utf-8"))
    return Truth(name=name, course_url=data["course_url"], term=data.get("term", ""),
                 items=[Expected(**item) for item in data.get("item", [])])


def course_names(evals_dir: Path = EVALS_DIR) -> list[str]:
    return sorted(p.parent.name for p in evals_dir.glob("*/truth.toml"))


# ---------- scoring ----------


def match_key(url: str) -> str:
    """The resource a URL names, without a repository branch: which branch holds the chosen term
    is the term's business, scored on its own."""
    key = identify(url).key
    return re.sub(r"@[^:]+", "", key) if key.startswith(("github:", "gitlab:")) else key


def _ratio(hit: int, total: int) -> str:
    return f"{hit}/{total} ({hit / total:.0%})" if total else "-"


def score_plan(truth: Truth, state: dict, *, pages_read: int | None = None) -> dict:
    """Compare a run's plan with the answer key. Items are matched by resource (match_key), so an
    arXiv /abs/ link and its /pdf/ link are the same item."""
    expected = {match_key(item.url): item for item in truth.items}
    entries = list(state["plan"].entries) if state.get("plan") else []
    found: dict[str, dict] = {}
    extras, duplicates, wrong_module, wrong_role = [], [], [], []
    for entry in entries:
        key = match_key(entry.task.url)
        planned = {"name": entry.task.name, "url": entry.task.url, "module": entry.module_key or "",
                   "role": entry.role}
        item = expected.get(key)
        if item is None:
            extras.append(planned)
        elif key in found:
            duplicates.append(planned)
        else:
            found[key] = planned
            if item.module is not None and planned["module"] != item.module:
                wrong_module.append({**planned, "expected": item.module})
            roles = [item.role] if isinstance(item.role, str) else item.role
            if roles is not None and planned["role"] not in roles:
                wrong_role.append({**planned, "expected": " or ".join(roles)})

    required = [key for key, item in expected.items() if not item.optional]
    optional = [key for key, item in expected.items() if item.optional]
    missing = [{"name": expected[key].name, "url": expected[key].url} for key in required if key not in found]
    matched_required = sum(1 for key in required if key in found)
    brief = state.get("brief")
    term = getattr(brief, "term", "") if brief else ""
    return {
        "course": truth.name,
        "planned": len(entries),
        "recall": matched_required / len(required) if required else None,
        "required_found": matched_required,
        "required": len(required),
        "optional_found": sum(1 for key in optional if key in found),
        "optional": len(optional),
        "precision": (len(entries) - len(extras)) / len(entries) if entries else None,
        "module_correct": len(found) - len(wrong_module),
        "role_correct": len(found) - len(wrong_role),
        "matched": len(found),
        "term": term,
        "term_expected": truth.term,
        "term_ok": None if not truth.term else truth.term.lower() in term.lower(),
        "tokens": sum((state.get("usage") or {}).values()),
        "pages_read": pages_read,
        "missing": missing,
        "extras": extras,
        "duplicates": duplicates,
        "wrong_module": wrong_module,
        "wrong_role": wrong_role,
    }


def report(score: dict, run_dir: Path | str = "") -> str:
    term = "not scored" if score["term_ok"] is None else (
        f"{'ok' if score['term_ok'] else 'WRONG'}: {score['term'] or '(none)'}"
        + ("" if score["term_ok"] else f", expected {score['term_expected']}"))
    lines = [
        f"# Eval: {score['course']}" + (f" ({run_dir})" if run_dir else ""), "",
        "| | |", "|---|---|",
        f"| Recall (required items planned) | {_ratio(score['required_found'], score['required'])} |",
        f"| Optional items planned | {score['optional_found']}/{score['optional']} |",
        f"| Precision (planned items on the key) | {_ratio(score['planned'] - len(score['extras']), score['planned'])} |",
        f"| Right module | {_ratio(score['module_correct'], score['matched'])} |",
        f"| Right role | {_ratio(score['role_correct'], score['matched'])} |",
        f"| Term | {term} |",
        f"| Tokens | {score['tokens']:,} |",
    ]
    if score["pages_read"] is not None:
        lines.append(f"| Pages read | {score['pages_read']} |")

    def section(title: str, rows: list[dict], show) -> None:
        if rows:
            lines.extend(["", f"## {title} ({len(rows)})", ""] + [f"- {show(r)}" for r in rows])

    section("Missing", score["missing"], lambda r: f"{r['name'] or r['url']} <{r['url']}>")
    section("Not on the answer key", score["extras"],
            lambda r: f"{r['name']} <{r['url']}> ({r['role']}, module {r['module'] or '-'})")
    section("Planned twice", score["duplicates"], lambda r: f"{r['name']} <{r['url']}>")
    section("Wrong module", score["wrong_module"],
            lambda r: f"{r['name']}: {r['module'] or 'course-wide'}, expected {r['expected'] or 'course-wide'}")
    section("Wrong role", score["wrong_role"], lambda r: f"{r['name']}: {r['role']}, expected {r['expected']}")
    return "\n".join(lines) + "\n"


# ---------- runs ----------


def run_state(run_dir: Path, course_url: str) -> dict:
    """The final graph state of the run in run_dir."""
    from .graph import build_graph
    from .graph.run import checkpointer
    from .state import make_task_id

    db = run_dir / STATE_DIRNAME / "checkpoints.sqlite"
    if not db.exists():
        raise FileNotFoundError(f"no run in {run_dir} ({db} is missing)")
    saver, conn = checkpointer(db)
    try:
        app = build_graph().compile(checkpointer=saver)
        return app.get_state({"configurable": {"thread_id": make_task_id(course_url)}}).values
    finally:
        conn.close()


def pages_read(run_dir: Path) -> int | None:
    """Distinct pages the agents read in the run (a page shown to several agents counts once)."""
    events = run_dir / STATE_DIRNAME / "events.jsonl"
    if not events.exists():
        return None
    return len({json.loads(line)["url"] for line in events.read_text(encoding="utf-8").splitlines()
                if '"type": "page"' in line})


def score_run(name: str, run_dir: Path, evals_dir: Path = EVALS_DIR) -> dict:
    truth = load_truth(name, evals_dir)
    state = run_state(run_dir, truth.course_url)
    if not state.get("plan"):
        raise ValueError(f"the run in {run_dir} has no plan for {truth.course_url}")
    score = score_plan(truth, state, pages_read=pages_read(run_dir))
    (run_dir / "eval.json").write_text(json.dumps(score, indent=1, ensure_ascii=False), encoding="utf-8")
    (run_dir / "eval.md").write_text(report(score, run_dir), encoding="utf-8")
    return score


def run_eval(name: str, *, llm=None, token_budget: int = RUN_TOKEN_BUDGET, on_event=None,
             evals_dir: Path = EVALS_DIR, runs_dir: Path = RUNS_DIR) -> tuple[dict, Path]:
    """Plan the saved course site with the agents, then score the plan."""
    from .graph import run_course

    truth = load_truth(name, evals_dir)
    run_dir = runs_dir / f"{name}-{datetime.now():%Y%m%d-%H%M%S}"
    run_course(truth.course_url, run_dir, llm=llm, on_event=on_event, token_budget=token_budget, plan_only=True,
               saved_pages=evals_dir / name / "pages.json.gz")
    return score_run(name, run_dir, evals_dir), run_dir


def _summary_row(score: dict) -> str:
    pct = lambda v: "-" if v is None else f"{v:.0%}"  # noqa: E731
    term = {None: "-", True: "ok", False: "WRONG"}[score["term_ok"]]
    return (f"| {score['course']} | {pct(score['recall'])} | {pct(score['precision'])} | "
            f"{score['module_correct']}/{score['matched']} | {score['role_correct']}/{score['matched']} | "
            f"{term} | {score['tokens']:,} |")


def main(argv: list[str] | None = None) -> int:
    from .__main__ import _printer
    from .llm import LLMRouter, parse_model_specs

    parser = argparse.ArgumentParser(prog="python -m scraper.evals", description="Score plans against answer keys.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list the courses in the eval set")
    run = sub.add_parser("run", help="plan saved course sites with the agents and score the plans")
    run.add_argument("names", nargs="+", help="course names, or 'all'")
    run.add_argument("--model", action="append", default=[], metavar="ROLE=ENDPOINT:MODEL")
    run.add_argument("--budget", type=int, default=RUN_TOKEN_BUDGET)
    run.add_argument("--ask-token", action="store_true", help="prompt for a HuggingFace token (never saved)")
    run.add_argument("-q", "--quiet", action="store_true", help="don't print what the agents do")
    score = sub.add_parser("score", help="score the plan of an existing run")
    score.add_argument("name")
    score.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)

    if args.command == "list":
        for name in course_names():
            truth = load_truth(name)
            required = sum(1 for i in truth.items if not i.optional)
            print(f"{name:28} {required:3} required items  {truth.course_url}")
        return 0
    if args.command == "score":
        result = score_run(args.name, args.run_dir)
        print(report(result, args.run_dir))
        return 0

    names = course_names() if args.names == ["all"] else args.names
    unknown = [n for n in names if n not in course_names()]
    if unknown:
        parser.error(f"not in the eval set: {', '.join(unknown)} (see: python -m scraper.evals list)")
    try:
        models = parse_model_specs(args.model)
    except ValueError as exc:
        parser.error(str(exc))
    api_keys = {"hf": getpass.getpass("HuggingFace token (input hidden): ").strip()} if args.ask_token else None
    llm = LLMRouter.from_config(api_keys=api_keys, models=models)
    rows = []
    for name in names:
        print(f"\n######## {name}")
        result, run_dir = run_eval(name, llm=llm, token_budget=args.budget, on_event=None if args.quiet else _printer)
        print("\n" + report(result, run_dir))
        rows.append(_summary_row(result))
    if len(rows) > 1:
        print("| Course | Recall | Precision | Module | Role | Term | Tokens |\n|---|---|---|---|---|---|---|")
        print("\n".join(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
