"""Streamlit UI. Run: streamlit run app.py"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx

from scraper.config import HF_MODEL
from scraper.models import DownloadResult, PlanItem
from scraper.pipeline import run as run_pipeline

LOG_FILENAME = "coursepull.log"

st.set_page_config(page_title="CoursePull", layout="wide")
st.title("Course Resource Downloader")
st.caption("3-agent pipeline: Discoverer → Classifier → Verifier · all on HF Inference.")


def _setup_file_logging(log_path: Path) -> None:
    handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("scraper")
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler):
            root.removeHandler(h)
            h.close()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    root.propagate = False


def _open_folder(path: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


with st.sidebar:
    st.header("Inputs")
    course_url = st.text_input("Course URL", placeholder="https://...")
    output_dir_str = st.text_input(
        "Output directory (absolute path)",
        placeholder=r"E:\CourseDownloads\my_course",
    )
    hf_token = st.text_input("HuggingFace token", type="password")
    st.caption(f"Model: `{HF_MODEL}` (edit in `scraper/config.py`)")
    start = st.button("Start", type="primary", use_container_width=True)


if start:
    if not course_url.strip() or not output_dir_str.strip():
        st.error("Course URL and output directory are both required.")
        st.stop()
    if not hf_token.strip():
        st.error("HuggingFace token is required.")
        st.stop()
    output_dir = Path(output_dir_str.strip())
    if not output_dir.parent.exists():
        st.error(f"Parent directory does not exist: {output_dir.parent}")
        st.stop()

    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / LOG_FILENAME
    _setup_file_logging(log_path)

    st.caption(f"Full log: `{log_path}`")
    log_box = st.empty()
    progress_bar = st.progress(0.0, text="Starting...")
    log_lines: list[str] = []
    main_ctx = get_script_run_ctx()

    def _ensure_ctx() -> None:
        # Download workers (ThreadPoolExecutor) call back here. Attach the main
        # script's Streamlit context once per thread so st.* calls don't warn.
        t = threading.current_thread()
        if main_ctx is not None and not getattr(t, "_cp_ctx", False):
            add_script_run_ctx(t, main_ctx)
            t._cp_ctx = True  # type: ignore[attr-defined]

    def log(msg: str) -> None:
        _ensure_ctx()
        log_lines.append(msg)
        log_box.code("\n".join(log_lines[-300:]), language="text")

    def on_progress(idx: int, total: int, item: PlanItem, result: DownloadResult) -> None:
        _ensure_ctx()
        glyph = "✓" if result.ok else ("·" if result.action == "skip" else "✗")
        short_url = item.url if len(item.url) <= 80 else item.url[:77] + "..."
        progress_bar.progress(idx / total, text=f"[{idx}/{total}] {glyph} {item.action} {short_url}")
        line = f"  [{idx}/{total}] {glyph} {item.action} {item.url}"
        if not result.ok and result.message:
            line += f"  ({result.message})"
        log(line)

    try:
        results = run_pipeline(
            url=course_url.strip(),
            output_dir=output_dir,
            hf_token=hf_token.strip(),
            log_callback=log,
            progress_callback=on_progress,
        )
    except Exception as exc:  # noqa: BLE001
        st.exception(exc)
        st.stop()

    progress_bar.empty()
    ok = sum(1 for r in results if r.ok)
    st.session_state["last_dir"] = str(output_dir)
    st.session_state["last_summary"] = f"{ok}/{len(results)} succeeded"


last_dir = st.session_state.get("last_dir")
if last_dir:
    out = Path(last_dir)
    st.success(st.session_state.get("last_summary", "Done."))

    readme = out / "README.md"
    if readme.exists():
        with st.expander("README.md", expanded=True):
            st.markdown(readme.read_text(encoding="utf-8"))

    skipped = out / "skipped.md"
    if skipped.exists():
        with st.expander("skipped.md", expanded=False):
            st.markdown(skipped.read_text(encoding="utf-8"))

    log_file = out / LOG_FILENAME
    if log_file.exists():
        st.caption(f"Full log: `{log_file}`")

    if st.button("Open folder"):
        _open_folder(out)
