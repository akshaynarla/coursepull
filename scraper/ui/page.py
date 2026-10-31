"""The NiceGUI page: start a run, watch the agents work, see the course fill in.

One run at a time for the whole app (it is a local, single-user tool). Every open
browser tab shows the same run; reloading the page mid-run picks up where it is.

The stat tiles, agent list, course map and feed are HTML strings rather than a tree of
widgets: a 70-item map redraws as one element, and every text in them (course pages,
agent output) is escaped before it is shown.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import urlparse

from nicegui import app, ui

from ..config import HF_MODEL, RUN_TOKEN_BUDGET
from ..llm import parse_model_specs
from .controller import RunController
from .view import AGENTS, PHASES, Item, Line, RunView, short_name

controller = RunController()

ROLE_LABELS = {
    "lecture_slides": "slides", "lecture_notes": "notes", "lecture_video": "video", "reading": "reading",
    "assignment": "assignment", "solution": "solution", "exam": "exam", "project": "project", "code": "code",
    "dataset": "data", "syllabus": "syllabus", "other": "other",
}
STATUS_ICONS = {"planned": "radio_button_unchecked", "queued": "schedule", "done": "check_circle",
                "failed": "error", "cancelled": "block"}  # running shows a spinner
RUN_STATUS = {"idle": ("Ready", "idle"), "running": ("Running", "running"), "stopping": ("Stopping…", "warn"),
              "finished": ("Finished", "ok"), "error": ("Error", "bad")}
AGENT_ICONS = {"orchestrator": "hub", "scouts": "travel_explore", "tutor": "school", "reviewer": "rate_review",
               "engine": "download", "examiner": "fact_check", "finish": "flag"}
_FEED_AGENTS = (("Orchestrator", "orchestrator"), ("Scout", "scouts"), ("Tutor", "tutor"), ("Reviewer", "reviewer"),
                ("Download", "engine"), ("Examiner", "examiner"))
MAP_REFRESH_S = 1.0  # the course map is the heaviest part: redraw it at most this often
FEED_MAX_LINES = 500

STYLE = """
:root {
  --bg:#f4f5f9; --surface:#ffffff; --surface-2:#eef0f5; --border:#e2e5ee; --text:#151821; --muted:#5f6679;
  --faint:#9aa0b0; --accent:#4f46e5; --on-accent:#ffffff; --ok:#059669; --warn:#d97706; --bad:#dc2626;
  --a-orchestrator:#7c3aed; --a-scouts:#0891b2; --a-tutor:#b45309; --a-reviewer:#db2777; --a-engine:#4f46e5;
  --a-examiner:#059669; --a-finish:#475569; --a-app:#64748b;
  --r-slides:#4f46e5; --r-video:#e11d48; --r-reading:#b45309; --r-assignment:#059669; --r-solution:#0d9488;
  --r-code:#0891b2; --r-notes:#475569; --r-exam:#dc2626; --r-project:#7c3aed; --r-data:#0284c7; --r-other:#64748b;
  --shadow:0 1px 2px rgba(16,24,40,.04),0 1px 3px rgba(16,24,40,.06);
  --mono:'Cascadia Mono','JetBrains Mono',Consolas,ui-monospace,monospace;
}
body.body--dark {
  --bg:#0a0c11; --surface:#12151d; --surface-2:#1a1e29; --border:#242938; --text:#e6e8ef; --muted:#959cb0;
  --faint:#5b6275; --accent:#8b8dff; --on-accent:#0a0c11; --ok:#34d399; --warn:#fbbf24; --bad:#f87171;
  --a-orchestrator:#a78bfa; --a-scouts:#22d3ee; --a-tutor:#fbbf24; --a-reviewer:#f472b6; --a-engine:#8b8dff;
  --a-examiner:#34d399; --a-finish:#94a3b8; --a-app:#94a3b8;
  --r-slides:#a5b4fc; --r-video:#fb7185; --r-reading:#fcd34d; --r-assignment:#6ee7b7; --r-solution:#5eead4;
  --r-code:#67e8f9; --r-notes:#cbd5e1; --r-exam:#fca5a5; --r-project:#c4b5fd; --r-data:#7dd3fc; --r-other:#cbd5e1;
  --shadow:none;
}
body { background:var(--bg) !important; color:var(--text);
  font-family:'Segoe UI Variable Text','Segoe UI',Inter,system-ui,-apple-system,sans-serif;
  -webkit-font-smoothing:antialiased; }
.nicegui-content { padding:0; }
.material-icons { font-size:inherit; }

.q-header.cp-header { background:color-mix(in srgb,var(--surface) 88%,transparent); backdrop-filter:blur(10px);
  color:var(--text); border-bottom:1px solid var(--border); }
.cp-brand { display:flex; align-items:center; gap:10px; font-weight:700; font-size:17px; letter-spacing:-.01em;
  white-space:nowrap; }
.cp-brand .mark { width:30px; height:30px; border-radius:9px; display:grid; place-items:center; font-size:18px;
  color:#fff; background:linear-gradient(135deg,#6366f1,#a855f7); }
.cp-url { max-width:760px; }
.cp-token { width:15rem; flex:none; }
.cp-pill { display:inline-flex; align-items:center; gap:7px; padding:5px 11px; border-radius:99px; font-size:12px;
  font-weight:600; white-space:nowrap; border:1px solid var(--border); background:var(--surface-2); }
.cp-pill i { width:8px; height:8px; border-radius:50%; background:var(--faint); }
.cp-pill.running i { background:var(--accent); animation:cp-pulse 1.6s ease-in-out infinite; }
.cp-pill.warn i { background:var(--warn); } .cp-pill.ok i { background:var(--ok); } .cp-pill.bad i { background:var(--bad); }
@media (max-width:900px) { .cp-folder { display:none !important; } }
@media (max-width:1100px) {
  .q-header.cp-header { flex-wrap:wrap; row-gap:8px; }
  .cp-url { order:10; flex-basis:0; min-width:55%; max-width:none; }
  .cp-token { order:11; }
  .cp-brandbox { margin-right:auto; }
}
@media (max-width:760px) { .cp-url, .cp-token { flex-basis:100%; width:auto; } }
@media (max-width:640px) { .cp-brand .nm { display:none; } }

.cp-page { max-width:1680px; margin:0 auto; padding:16px 20px 28px; }
.cp-card { background:var(--surface); border:1px solid var(--border); border-radius:14px; box-shadow:var(--shadow); }
.cp-label { font-size:11px; font-weight:650; letter-spacing:.08em; text-transform:uppercase; color:var(--muted); }

.cp-stats { display:grid; gap:12px; grid-template-columns:repeat(2,minmax(0,1fr)); }
@media (min-width:768px) { .cp-stats { grid-template-columns:repeat(5,minmax(0,1fr)); } }
.cp-tile { padding:14px 16px; min-width:0; }
.cp-tile .v { font-size:22px; font-weight:650; letter-spacing:-.01em; margin-top:4px; font-variant-numeric:tabular-nums;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.cp-tile .s { font-size:12px; color:var(--muted); margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.cp-bar { height:6px; border-radius:99px; background:var(--surface-2); overflow:hidden; margin-top:10px; }
.cp-bar.thin { height:4px; margin-top:8px; }
.cp-bar > span { display:block; height:100%; border-radius:inherit; background:var(--accent); transition:width .5s ease; }
.cp-bar.ok > span { background:var(--ok); } .cp-bar.warn > span { background:var(--warn); }

.cp-main { display:grid; gap:16px; grid-template-columns:minmax(0,1fr); align-items:start; }
.cp-feedcol { height:440px; }
@media (min-width:1024px) { .cp-main { grid-template-columns:270px minmax(0,1fr); } .cp-feedcol { grid-column:1/-1; } }
@media (min-width:1280px) {
  .cp-main { grid-template-columns:270px minmax(0,1fr) 390px; }
  .cp-feedcol { grid-column:auto; height:calc(100vh - 92px); }
  .cp-sticky { position:sticky; top:76px; }
}
.cp-panel { padding:16px; }

.cp-agents { display:flex; flex-direction:column; gap:2px; margin-top:10px; }
.cp-agent { display:flex; gap:12px; align-items:flex-start; padding:8px; border-radius:10px; position:relative; }
.cp-agent:not(:last-child)::after { content:''; position:absolute; left:23px; top:42px; bottom:-8px; width:2px;
  border-radius:2px; background:var(--border); }
.cp-agent.done:not(:last-child)::after { background:color-mix(in srgb,var(--ok) 45%,var(--border)); }
.cp-node { width:32px; height:32px; flex:none; border-radius:9px; display:grid; place-items:center; font-size:18px;
  background:var(--surface-2); color:var(--faint); border:1px solid var(--border); }
.cp-agent.done .cp-node { color:var(--c); background:color-mix(in srgb,var(--c) 12%,transparent);
  border-color:color-mix(in srgb,var(--c) 32%,transparent); }
.cp-agent.active { background:color-mix(in srgb,var(--c) 11%,transparent); }
.cp-agent.active .cp-node { background:var(--c); border-color:var(--c); color:var(--on-accent);
  animation:cp-glow 1.8s ease-in-out infinite; }
.cp-agent .n { font-weight:600; font-size:13.5px; line-height:1.3; }
.cp-agent.idle .n { color:var(--muted); }
.cp-agent .d { font-size:12px; color:var(--muted); line-height:1.35; margin-top:1px; }
.cp-verdict { margin-top:10px; padding:10px 12px; border-radius:10px; border:1px solid var(--border);
  background:var(--surface-2); }
.cp-verdict .h { display:flex; align-items:center; gap:6px; font-size:12.5px; font-weight:650; }
.cp-verdict.ok .h { color:var(--ok); } .cp-verdict.warn .h { color:var(--warn); }
.cp-verdict .t { margin-top:4px; font-size:12.5px; line-height:1.45; color:var(--muted); display:-webkit-box;
  -webkit-line-clamp:4; -webkit-box-orient:vertical; overflow:hidden; }

.cp-maphead { display:flex; align-items:flex-end; justify-content:space-between; gap:12px; flex-wrap:wrap; margin:2px 2px 12px; }
.cp-maphead h2 { font-size:19px; font-weight:650; letter-spacing:-.01em; line-height:1.3; margin:4px 0 0; }
.cp-maphead .m { font-size:12.5px; color:var(--muted); white-space:nowrap; }
.cp-modules { display:grid; gap:12px; grid-template-columns:minmax(0,1fr); }
@media (min-width:1760px) { .cp-modules { grid-template-columns:repeat(2,minmax(0,1fr)); } }
.cp-module { padding:12px 14px 6px; }
.cp-module header { display:flex; align-items:center; gap:10px; }
.cp-module .k { font-family:var(--mono); font-size:11.5px; font-weight:600; color:var(--accent); padding:2px 7px;
  border-radius:6px; background:color-mix(in srgb,var(--accent) 12%,transparent); }
.cp-module .t { font-weight:600; font-size:14px; flex:1; min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.cp-module .c { font-size:12px; color:var(--muted); font-variant-numeric:tabular-nums; white-space:nowrap; }
.cp-module ul { list-style:none; margin:8px 0 0; padding:0; }
.cp-module li { border-top:1px solid var(--border); padding:6px 2px; }
.cp-row { display:flex; align-items:center; gap:10px; min-width:0; font-size:13px; }
.cp-row .st { font-size:17px; width:20px; flex:none; display:grid; place-items:center; color:var(--faint); }
.s-done .st { color:var(--ok); } .s-failed .st { color:var(--bad); } .s-queued .st { color:var(--muted); }
.s-cancelled { opacity:.6; }
.cp-row .nm { flex:1; min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.opt .nm { color:var(--muted); }
.cp-row .fmt { font-family:var(--mono); font-size:11px; color:var(--faint); white-space:nowrap; }
.cp-chip { font-size:11px; font-weight:600; padding:2px 8px; border-radius:99px; white-space:nowrap;
  color:var(--c); background:color-mix(in srgb,var(--c) 13%,transparent); }
.cp-chip.optional { --c:var(--muted); background:transparent; border:1px solid var(--border); font-weight:500; }
.cp-sub { font-size:12px; color:var(--muted); margin:2px 0 0 30px; }
.cp-sub.err { color:var(--bad); }
.cp-sub .cp-bar { margin:4px 0 2px; }
.cp-spin { width:14px; height:14px; border-radius:50%; border:2px solid color-mix(in srgb,var(--accent) 25%,transparent);
  border-top-color:var(--accent); animation:cp-rot .8s linear infinite; }

.cp-hero { padding:36px 32px; }
.cp-hero .eyebrow { font-size:12px; font-weight:650; letter-spacing:.08em; text-transform:uppercase; color:var(--accent); }
.cp-hero h1 { font-size:28px; line-height:1.2; font-weight:700; letter-spacing:-.02em; margin:10px 0 10px; max-width:640px; }
.cp-hero p { color:var(--muted); font-size:14.5px; line-height:1.6; max-width:640px; margin:0; }
.cp-steps { display:grid; gap:12px; margin-top:26px; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); }
.cp-step { padding:14px; border-radius:12px; background:var(--surface-2); border:1px solid var(--border); }
.cp-step .material-icons { font-size:20px; color:var(--c); }
.cp-step b { display:block; font-size:13.5px; margin-top:8px; }
.cp-step span { font-size:12.5px; color:var(--muted); line-height:1.45; }
.cp-wait { padding:48px 24px; text-align:center; color:var(--muted); font-size:14px; }
.cp-wait .cp-spin { width:26px; height:26px; border-width:3px; margin:0 auto 14px; }

.cp-feedcol { display:flex; flex-direction:column; min-height:0; padding:16px 6px 8px 16px; }
.cp-feed { flex:1; min-height:0; margin-top:8px; }
.cp-feed .q-scrollarea__content { gap:0; }
.cp-fl { display:grid; grid-template-columns:56px minmax(0,1fr); gap:8px; padding:4px 10px 4px 0; font-size:12.5px;
  line-height:1.45; }
.cp-fl .tm { font-family:var(--mono); font-size:11px; color:var(--faint); padding-top:1.5px; }
.cp-fl .who { font-weight:650; color:var(--c); margin-right:4px; }
.k-tool .tx, .k-page .tx { color:var(--muted); font-family:var(--mono); font-size:11.5px; word-break:break-all; }
.k-info .tx { color:var(--muted); } .k-error .tx { color:var(--bad); }
.cp-fl.long .bd { display:-webkit-box; -webkit-line-clamp:5; -webkit-box-orient:vertical; overflow:hidden; cursor:pointer; }
.cp-fl.long.open .bd { display:block; }
.cp-fphase { display:flex; align-items:center; gap:10px; margin:10px 10px 6px 0; font-size:11px; font-weight:650;
  letter-spacing:.08em; text-transform:uppercase; color:var(--accent); }
.cp-fphase::before, .cp-fphase::after { content:''; height:1px; flex:1; background:var(--border); }
.cp-fempty { color:var(--faint); font-size:13px; padding:24px 10px 0 0; text-align:center; }

.cp-finish { padding:14px 18px; border-left:4px solid var(--tone); }
.cp-finish.ok { --tone:var(--ok); } .cp-finish.warn { --tone:var(--warn); } .cp-finish.bad { --tone:var(--bad); }
.cp-finish .ic { font-size:30px; color:var(--tone); }
.cp-finish .h { font-size:16px; font-weight:650; }
.cp-finish .s { font-size:13px; color:var(--muted); margin-top:2px; }

@keyframes cp-pulse { 0%,100% { opacity:1; } 50% { opacity:.35; } }
@keyframes cp-rot { to { transform:rotate(360deg); } }
@keyframes cp-glow { 0%,100% { box-shadow:0 0 0 0 color-mix(in srgb,var(--c) 45%,transparent); }
  50% { box-shadow:0 0 0 6px color-mix(in srgb,var(--c) 0%,transparent); } }
"""


def default_folder(url: str) -> str:
    parsed = urlparse(url.strip())
    slug = re.sub(r"[^a-z0-9]+", "-", f"{parsed.netloc}{parsed.path}".lower()).strip("-") or "course"
    return str(Path("downloads") / slug[:60])


def fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}" if seconds < 3600 else f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def open_folder(path: str) -> None:
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", path], check=False)


# ---------- HTML for the parts of the page ----------


def _icon(name: str, cls: str = "") -> str:
    return f'<span class="material-icons {cls}">{name}</span>'


def _bar(fraction: float, tone: str = "", thin: bool = False) -> str:
    width = max(0.0, min(fraction, 1.0)) * 100
    return f'<div class="cp-bar {tone}{" thin" if thin else ""}"><span style="width:{width:.1f}%"></span></div>'


def status_pill(view: RunView) -> str:
    label, tone = RUN_STATUS.get(view.status, RUN_STATUS["idle"])
    if view.status == "running" and view.phase in PHASES:
        label = PHASES[view.phase]
    return f'<span class="cp-pill {tone}"><i></i>{escape(label)}</span>'


def stats_html(view: RunView) -> str:
    def tile(label: str, value: str, sub: str, extra: str = "") -> str:
        return (f'<div class="cp-card cp-tile"><div class="cp-label">{label}</div><div class="v">{escape(value)}</div>'
                f'<div class="s">{escape(sub)}</div>{extra}</div>')

    if view.status == "finished":
        now, now_sub = "Done", "the run is over"
    elif view.status == "error":
        now, now_sub = "Stopped", "with an error"
    elif view.active:
        now, now_sub = AGENTS[view.active][0], PHASES.get(view.phase, view.phase) or "working"
    else:
        now, now_sub = "Idle", "waiting for a course"

    total = len(view.items)
    downloading = any(i.status != "planned" for i in view.items.values())
    items_sub = (f"{view.failed} failed" if view.failed else ("downloaded" if downloading else "planned")) if total \
        else "no plan yet"
    items_bar = _bar(view.finished_items / total, "ok" if total and view.finished_items == total and not view.failed
                     else "") if total else _bar(0)

    data_sub = f"{view.summary['files']} files" if view.summary else "downloaded"
    started = datetime.fromtimestamp(view.started_at).strftime("started %H:%M") if view.started_at else "not started"

    limit = view.token_limit
    tokens_sub = f"of {limit / 1000:,.0f}k budget" if limit else "tokens used"
    share = view.tokens_used / limit if limit else 0
    return "".join([
        tile("Now", now, now_sub),
        tile("Items", f"{view.finished_items} / {total}" if total else "—", items_sub, items_bar),
        tile("Downloaded", fmt_bytes(view.bytes_done) if view.bytes_done else "—", data_sub),
        tile("Time", fmt_duration(view.elapsed), started),
        tile("Tokens", f"{view.tokens_used / 1000:,.0f}k", tokens_sub, _bar(share, "warn" if share > 0.8 else "")),
    ])


def agents_html(view: RunView) -> str:
    rows = []
    for node, (name, _) in AGENTS.items():
        rows.append(
            f'<div class="cp-agent {view.agent_state(node)}" style="--c:var(--a-{node})">'
            f'<div class="cp-node">{_icon(AGENT_ICONS[node])}</div>'
            f'<div><div class="n">{escape(name)}</div><div class="d">{escape(view.agent_caption(node))}</div></div></div>')
    return f'<div class="cp-label">Agents</div><div class="cp-agents">{"".join(rows)}</div>'


def item_html(item: Item) -> str:
    status = item.status if item.status in STATUS_ICONS or item.status == "running" else "planned"
    mark = '<span class="cp-spin"></span>' if status == "running" else _icon(STATUS_ICONS[status])
    name = short_name(item.name, item.module)
    formats = sorted({Path(f).suffix.lstrip(".").lower() for f in item.files if Path(f).suffix})[:3]
    role = ROLE_LABELS.get(item.role, item.role)
    row = (f'<div class="cp-row"><span class="st">{mark}</span>'
           f'<span class="nm" title="{escape(item.name)}">{escape(name)}</span>'
           + ("" if item.required else '<span class="cp-chip optional">optional</span>')
           + (f'<span class="fmt">{escape(" · ".join(formats))}</span>' if formats else "")
           + f'<span class="cp-chip" style="--c:var(--r-{escape(role)}, var(--r-other))">{escape(role)}</span></div>')
    note = f"{item.error}: {item.message}" if item.error else item.message
    if item.children:
        done = sum(1 for s in item.children.values() if s == "done")
        note = f"folder: {done}/{len(item.children)} files" + (f" · {note}" if note else "")
    sub = ""
    if note:
        sub += f'<div class="cp-sub{" err" if status == "failed" else ""}">{escape(note)}</div>'
    if status == "running" and (fraction := item.fraction) is not None:
        sub += f'<div class="cp-sub">{_bar(fraction, thin=True)}</div>'
    return f'<li class="s-{status}{"" if item.required else " opt"}">{row}{sub}</li>'


def map_html(view: RunView) -> str:
    if not view.items:
        if view.status in ("running", "stopping"):
            return ('<div class="cp-card cp-wait"><div class="cp-spin"></div>'
                    "The course map appears here once the Tutor has made a plan.</div>")
        return HERO
    downloading = any(i.status != "planned" for i in view.items.values())
    cards = []
    for heading, items in view.module_groups():
        key, _, title = heading.partition(" · ")
        if not title:
            key, title = "", heading
        title = short_name(title, key)  # "Week 1 - Intro" beside its "01" badge reads as "Intro"
        done = sum(1 for i in items if i.status == "done")
        failed = sum(1 for i in items if i.status == "failed")
        count = (f"{done}/{len(items)}" + (f" · {failed} failed" if failed else "")) if downloading \
            else f"{len(items)} planned"
        bar = _bar(done / len(items), "ok" if done == len(items) else "", thin=True) if downloading else ""
        cards.append(
            f'<section class="cp-card cp-module"><header>'
            + (f'<span class="k">{escape(key)}</span>' if key else "")
            + f'<span class="t" title="{escape(title)}">{escape(title)}</span><span class="c">{escape(count)}</span>'
            f'</header>{bar}<ul>{"".join(item_html(i) for i in items)}</ul></section>')
    meta = f"{len(view.items)} items · {len(view.modules)} modules · {view.excluded} left out"
    return (f'<div class="cp-maphead"><div><div class="cp-label">Course map</div><h2>{escape(view.title)}</h2></div>'
            f'<div class="m">{escape(meta)}</div></div><div class="cp-modules">{"".join(cards)}</div>')


HERO = (
    '<div class="cp-card cp-hero"><div class="eyebrow">Multi-agent course collector</div>'
    "<h1>Point it at an open course. Get everything you need to study it.</h1>"
    "<p>Paste a course's web address above and press Start. The agents work out what the course needs, "
    "plan it module by module and download it, then check that every file is what it should be.</p>"
    '<div class="cp-steps">'
    + "".join(
        f'<div class="cp-step" style="--c:var(--a-{node})">{_icon(icon)}<b>{name}</b><span>{text}</span></div>'
        for node, icon, name, text in (
            ("scouts", "travel_explore", "Explore", "Scouts read the course pages and report every material."),
            ("tutor", "school", "Plan", "The Tutor builds a course map; the Reviewer critiques it."),
            ("engine", "download", "Download", "Slides, videos, papers, code and Drive folders, in parallel."),
            ("examiner", "fact_check", "Verify", "The Examiner opens the files and recovers what failed."),
        ))
    + "</div></div>"
)


def line_html(line: Line) -> str:
    if line.kind == "phase":
        word, _, rest = line.text.partition(" ")
        return f'<div class="cp-fphase">{escape(PHASES.get(word, word))}{" " + escape(rest) if rest else ""}</div>'
    key = next((k for prefix, k in _FEED_AGENTS if line.agent.startswith(prefix)), "app")
    who = f'<span class="who">{escape(line.agent)}</span>' if line.agent else ""
    stamp = datetime.fromtimestamp(line.ts).strftime("%H:%M:%S")
    long = ' long" title="Click to show all" onclick="this.classList.toggle(\'open\')' if len(line.text) > 320 else ""
    return (f'<div class="cp-fl k-{line.kind}{long}" style="--c:var(--a-{key})"><span class="tm">{stamp}</span>'
            f'<div class="bd">{who}<span class="tx">{escape(line.text)}</span></div></div>')


# ---------- interactive pieces ----------


def verdicts(view: RunView) -> None:
    for agent, verdict, good in (("Reviewer", view.review, "approved"), ("Examiner", view.examination, "passed")):
        if not verdict:
            continue
        ok = bool(verdict.get(good))
        title = f"{agent} {'approved the plan' if agent == 'Reviewer' else 'passed the result'}" if ok else (
            f"{agent} {'asked for changes' if agent == 'Reviewer' else 'did not pass the result'}")
        summary = verdict.get("summary", "")
        with ui.element("div").classes(f"cp-verdict {'ok' if ok else 'warn'}"):
            ui.html(f'<div class="h">{_icon("verified" if ok else "report")}{escape(title)}</div>'
                    f'<div class="t">{escape(summary)}</div>', sanitize=False)
            if len(summary) > 220:
                ui.button("Read all", on_click=lambda t=title, s=summary: show_text(t, s)).props(
                    "flat dense no-caps size=sm").classes("mt-1 -ml-2")


def finish_card(view: RunView) -> None:
    if view.status == "error":
        with ui.element("div").classes("cp-card cp-finish bad w-full"):
            ui.html(f'<div class="flex items-center gap-3">{_icon("error", "ic")}<div><div class="h">'
                    f'The run stopped with an error</div><div class="s">{escape(view.error)}</div></div></div>',
                    sanitize=False)
        return
    if view.status != "finished" or not view.summary:
        return
    s = view.summary
    tone = "ok" if s.get("passed") is not False and not s["failed"] and not s.get("stop_reason") else "warn"
    notes = []
    if s.get("stop_reason"):
        notes.append({"cancelled": "Stopped by you.", "budget": "The token budget ran out."}.get(
            s["stop_reason"], f"Stopped early: {s['stop_reason']}."))
    if s.get("passed") is not None:
        notes.append("The Examiner passed the result." if s["passed"] else "The Examiner did not pass the result.")
    notes.append(f"{fmt_bytes(s['bytes'])} in {fmt_duration(view.elapsed)}, {view.tokens_used:,} tokens.")
    headline = f"{s['files']} files from {s['planned']} planned items" + (f", {s['failed']} failed" if s["failed"] else "")
    folder = view.output_dir
    with ui.element("div").classes(f"cp-card cp-finish {tone} w-full"):
        with ui.row().classes("w-full items-center gap-4"):
            ui.html(f'<div class="flex items-center gap-3">{_icon("task_alt" if tone == "ok" else "info", "ic")}'
                    f'<div><div class="h">{escape(headline)}</div><div class="s">{escape(" ".join(notes))}</div>'
                    f'</div></div>', sanitize=False)
            ui.space()
            with ui.row().classes("gap-2"):
                ui.button("Open folder", icon="folder_open", on_click=lambda: open_folder(folder)).props(
                    "unelevated no-caps")
                ui.button("Course index", icon="menu_book",
                          on_click=lambda: show_file(Path(folder) / "README.md")).props("outline no-caps")
                ui.button("Left out & failed", icon="playlist_remove",
                          on_click=lambda: show_file(Path(folder) / "skipped.md")).props("outline no-caps")
                if s["failed"]:
                    url = view.course_url
                    ui.button(f"Retry {s['failed']} failed", icon="replay",
                              on_click=lambda: controller.retry(url, folder)).props(
                        "outline no-caps color=orange-8").tooltip(
                        "Download the failed items again with the same plan: no agents, no tokens")


def show_file(path: Path) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else f"{path.name} has not been written yet."
    with ui.dialog() as dialog, ui.card().classes("w-[56rem] max-w-full"):
        with ui.scroll_area().classes("h-[70vh]"):
            ui.markdown(text)
        ui.button("Close", on_click=dialog.close).props("flat no-caps")
    dialog.open()


def show_text(title: str, text: str) -> None:
    with ui.dialog() as dialog, ui.card().classes("w-[44rem] max-w-full"):
        ui.label(title).classes("text-base font-semibold")
        ui.label(text).classes("text-sm whitespace-pre-wrap")
        ui.button("Close", on_click=dialog.close).props("flat no-caps")
    dialog.open()


# ---------- the page ----------


@ui.page("/", title="CoursePull")
def index() -> None:
    ui.add_css(STYLE)
    ui.colors(primary="#6366f1")
    dark = ui.dark_mode(app.storage.general.get("dark", True))
    shown: dict = {"lines": 0, "versions": {}, "map_at": 0.0, "view": None, "html": {}}

    with ui.dialog() as options, ui.card().classes("w-[44rem] max-w-full gap-3"):
        ui.label("Run options").classes("text-base font-semibold")
        folder = ui.input("Save to folder", placeholder="downloads/<course>").props("outlined dense").classes("w-full")
        with ui.row().classes("w-full items-center gap-4"):
            plan_only = ui.switch("Plan only (download nothing)")
            budget = ui.number("Token budget", value=RUN_TOKEN_BUDGET, min=10_000, step=100_000,
                               format="%.0f").props("outlined dense").classes("w-44")
        model = ui.input("Model for all agents", value=f"hf:{HF_MODEL}").props("outlined dense").classes("w-full")
        with ui.grid(columns=2).classes("w-full gap-2"):
            per_role = {role: ui.input(f"{role.title()} model", placeholder="same as above").props("outlined dense")
                        for role in ("orchestrator", "scout", "tutor", "reviewer", "examiner")}
        with ui.row().classes("w-full justify-end"):
            ui.button("Done", on_click=options.close).props("unelevated no-caps")

    with ui.header(wrap=False).classes("cp-header items-center gap-3 px-4 py-2"):
        ui.html(f'<div class="cp-brand"><span class="mark">{_icon("school")}</span>'
                f'<span class="nm">CoursePull</span></div>', sanitize=False).classes("cp-brandbox")
        url = ui.input(placeholder="Paste an open course URL: https://…").props(
            "outlined dense clearable").classes("cp-url grow")
        hf_token = ui.input(placeholder="HuggingFace token", password=True, password_toggle_button=True).props(
            "outlined dense").classes("cp-token").tooltip(
            "Used for this run only, never saved. Leave empty to use HF_TOKEN or your huggingface-cli login.")
        folder_btn = ui.button("", icon="folder_open", on_click=options.open).props(
            "flat no-caps dense").classes("cp-folder text-xs opacity-80").tooltip("Save to folder (change in Options)")
        start_btn = ui.button("Start", icon="play_arrow").props("unelevated no-caps")
        stop_btn = ui.button("Stop", icon="stop").props("flat no-caps color=negative")
        ui.button("Options", icon="tune", on_click=options.open).props("flat no-caps dense")
        theme_btn = ui.button(icon="light_mode" if dark.value else "dark_mode").props("flat round dense").tooltip(
            "Light or dark")
        status = ui.html("", sanitize=False)

    def toggle_theme() -> None:
        dark.value = not dark.value
        app.storage.general["dark"] = dark.value
        theme_btn.props(f"icon={'light_mode' if dark.value else 'dark_mode'}")

    theme_btn.on_click(toggle_theme)

    def set_folder(value: str) -> None:
        folder.set_value(value)
        folder_btn.set_text(value)

    url.on_value_change(lambda e: set_folder(default_folder(e.value)) if e.value else None)
    folder.on_value_change(lambda e: folder_btn.set_text(e.value or ""))

    with ui.column().classes("cp-page w-full gap-4"):
        stats = ui.html("", sanitize=False).classes("cp-stats w-full")
        finish = ui.column().classes("w-full empty:hidden")
        with ui.element("div").classes("cp-main w-full"):
            with ui.element("div").classes("cp-card cp-panel cp-sticky"):
                agents = ui.html("", sanitize=False)
                verdict_box = ui.column().classes("w-full gap-0")
            course = ui.html("", sanitize=False).classes("min-w-0")
            with ui.element("div").classes("cp-card cp-feedcol cp-sticky"):
                ui.html('<div class="cp-label">Live feed</div>', sanitize=False)
                feed = ui.scroll_area().classes("cp-feed")

    def start() -> None:
        if not (url.value or "").startswith(("http://", "https://")):
            ui.notify("Enter the course's web address (http:// or https://)", type="warning")
            return
        try:
            specs = [f"all={model.value.strip()}"] + [f"{role}={box.value.strip()}" for role, box in
                                                      per_role.items() if (box.value or "").strip()]
            models = parse_model_specs(specs)
        except ValueError as exc:
            ui.notify(str(exc), type="warning")
            return
        controller.start(url.value.strip(), (folder.value or default_folder(url.value)).strip(),
                         models=models, hf_token=(hf_token.value or "").strip(), plan_only=plan_only.value,
                         token_budget=int(budget.value or RUN_TOKEN_BUDGET))
        ui.notify("Started", type="positive")

    start_btn.on_click(start)
    stop_btn.on_click(lambda: (controller.stop(), ui.notify("Stopping after the current steps…")))

    def set_html(element, key: str, html: str) -> None:
        if shown["html"].get(key) != html:
            shown["html"][key] = html
            element.set_content(html)

    def redraw(view: RunView, part: str, container, build) -> None:
        key = (part, build.__name__)
        if shown["versions"].get(key) != view.versions[part]:
            shown["versions"][key] = view.versions[part]
            container.clear()
            with container:
                build(view)

    def reset_feed() -> None:
        feed.clear()
        with feed:
            ui.html('<div class="cp-fempty">What the agents say and do appears here.</div>', sanitize=False)

    def tick() -> None:
        controller.pump()
        view = controller.view
        if shown["view"] is not view:  # a new run: start the page over
            shown.update(view=view, lines=0, versions={}, map_at=0.0, html={})
            reset_feed()

        set_html(status, "status", status_pill(view))
        start_btn.set_enabled(not controller.running)
        stop_btn.set_enabled(controller.running and view.status == "running")

        if shown.pop("scroll", False):  # a tick after the lines went in, once the browser has laid them out
            # Straight to the DOM: scroll_to's setScrollPercentage call was not applied here (NiceGUI 3.18).
            ui.run_javascript(f'const c = document.querySelector("#c{feed.id} .q-scrollarea__container");'
                              " if (c) c.scrollTop = c.scrollHeight;")
        new_lines = view.lines[shown["lines"]:]
        if new_lines:
            if shown["lines"] == 0:
                feed.clear()
            with feed:
                for line in new_lines:
                    ui.html(line_html(line), sanitize=False)
            children = feed.default_slot.children
            for old in children[:max(0, len(children) - FEED_MAX_LINES)]:
                feed.remove(old)
            shown["lines"] = len(view.lines)
            shown["scroll"] = True

        set_html(stats, "stats", stats_html(view))
        set_html(agents, "agents", agents_html(view))
        redraw(view, "status", verdict_box, verdicts)
        if time.time() - shown["map_at"] >= MAP_REFRESH_S or view.status in ("finished", "error"):
            shown["map_at"] = time.time()
            set_html(course, "map", map_html(view))
        redraw(view, "status", finish, finish_card)

    ui.timer(0.3, tick)


def serve(*, port: int = 8080, native: bool = False, show: bool = True, replay: Path | None = None,
          speed: float = 10.0) -> None:
    if native:
        try:
            import webview  # noqa: F401
        except ImportError:
            print("The native window needs pywebview (uv add pywebview); opening the browser instead.")
            native = False
    if replay is not None:
        started = []

        def replay_on_first_visit() -> None:  # start when someone is watching, not at server start
            if not started:
                started.append(True)
                controller.replay(replay, speed)

        app.on_connect(replay_on_first_visit)
    # Local only: the app controls downloads on this machine and is never exposed to the network.
    ui.run(host="127.0.0.1", port=port, title="CoursePull", native=native, show=show, reload=False,
           favicon="🎓", dark=None)
