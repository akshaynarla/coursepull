"""Code: GitHub repos/folders/files, GitLab, gists.

Term-aware GitHub links: courses often keep each offering on its own branch (e.g.
`fall2025`) while their pages link to `master`, which moves on to the next term. When
the run collects one term and a link points at the default branch, the branch or tag
named after that term is used if it exists.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import zipfile
from pathlib import Path
from urllib.parse import quote

from ..config import REQUEST_TIMEOUT
from ..state import ErrorKind
from .context import HandlerResult, TaskContext
from .errors import DownloadError, error_for_status
from .http import download, session

log = logging.getLogger(__name__)

DEFAULT_REFS = {"HEAD", "master", "main"}
_SEASONS = {
    "fall": ("fall", "autumn", "fa", "au", "f"), "autumn": ("autumn", "fall", "au", "fa", "f"),
    "spring": ("spring", "sp", "s"), "summer": ("summer", "su"), "winter": ("winter", "wi", "w"),
}
_TERM = re.compile(r"^\s*(fall|autumn|spring|summer|winter)[\s_-]*((?:19|20)\d\d)\s*$", re.IGNORECASE)
_ref_cache: dict[tuple[str, str], dict[str, str]] = {}
_ref_lock = threading.Lock()


def matching_ref(names: list[str], term: str) -> str | None:
    """The branch or tag named after a term ("Fall 2025" -> fall2025, fall-2025, fa25, 2025-fall)."""
    m = _TERM.match(term or "")
    if not m:
        return None
    season, year = m.group(1).lower(), m.group(2)
    tokens = {f"{a}{b}" for s in _SEASONS[season] for y in (year, year[2:]) for a, b in ((s, y), (y, s))}
    candidates = []
    for name in names:
        norm = re.sub(r"[^a-z0-9]", "", name.lower())
        if norm in tokens:
            candidates.append((0, len(name), name))  # exactly the term
        elif any(t in norm for t in tokens if len(t) >= 5):
            candidates.append((1, len(name), name))  # e.g. "release-fall2025"
    return min(candidates)[2] if candidates else None


def _github_refs(owner: str, repo: str) -> dict[str, str]:
    """Branch and tag names -> "heads" or "tags" (a branch wins when both share a name)."""
    key = (owner.lower(), repo.lower())
    with _ref_lock:
        if key in _ref_cache:
            return _ref_cache[key]
    refs: dict[str, str] = {}
    for kind, namespace in (("branches", "heads"), ("tags", "tags")):
        try:
            resp = session().get(f"https://api.github.com/repos/{owner}/{repo}/{kind}", params={"per_page": 100},
                                 timeout=REQUEST_TIMEOUT, headers={"Accept": "application/vnd.github+json"})
        except Exception as exc:  # noqa: BLE001 - no term branch is a fine fallback
            log.info("could not list %s of %s/%s: %s", kind, owner, repo, exc)
            continue
        if resp.status_code == 200:
            for item in resp.json():
                refs.setdefault(item["name"], namespace)
        else:
            log.info("listing %s of %s/%s: HTTP %s", kind, owner, repo, resp.status_code)
    with _ref_lock:
        _ref_cache[key] = refs
    return refs


def download_repo(ctx: TaskContext) -> HandlerResult:
    ref, params = ctx.ref, ctx.ref.params
    if ref.provider == "github":
        return _github(ctx, params)
    if params.get("blob") and ref.download_url:
        return HandlerResult([download(ref.download_url, ctx.claim, progress=ctx.progress, cancel=ctx.cancel)])
    if ref.provider == "gitlab":
        project = params["project"]
        url = f"https://gitlab.com/api/v4/projects/{quote(project, safe='')}/repository/archive.zip"
        if params.get("ref"):
            url += f"?sha={quote(params['ref'])}"
        return _archive(ctx, url, params.get("path"), default_stem=project.rsplit("/", 1)[-1])
    if ref.provider == "gist":
        return _gist(ctx, params["gist"])
    raise DownloadError(ErrorKind.UNSUPPORTED, f"no downloader for {ref.provider} repositories")


def _github(ctx: TaskContext, params: dict[str, str]) -> HandlerResult:
    owner, repo = params["owner"], params["repo"]
    linked = params.get("ref") or "HEAD"  # HEAD = the default branch, no API call needed
    refs = [(linked, linked)]  # (name, path used in URLs)
    if ctx.task.term and linked in DEFAULT_REFS:
        known = _github_refs(owner, repo)
        if term_ref := matching_ref(list(known), ctx.task.term):
            # Spell out refs/heads/ or refs/tags/: a branch and a tag may share the name, and
            # GitHub then answers an ambiguous archive URL with "300 Multiple Choices".
            refs = [(term_ref, f"refs/{known[term_ref]}/{term_ref}"), (linked, linked)]
    for i, (ref_name, ref_path) in enumerate(refs):
        try:
            if params.get("blob"):
                url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref_path}/{params['path']}"
                result = HandlerResult([download(url, ctx.claim, progress=ctx.progress, cancel=ctx.cancel)])
            else:
                url = f"https://github.com/{owner}/{repo}/archive/{ref_path}.zip"
                result = _archive(ctx, url, params.get("path"), default_stem=repo)
        except DownloadError as exc:
            if exc.kind != ErrorKind.NOT_FOUND or i == len(refs) - 1:
                raise
            continue  # the term's branch lacks this file or folder: fall back to the linked version
        if ref_name != linked:
            result.message = f"{ctx.task.term} version, from the {ref_name!r} branch (the link points to {linked})"
        elif len(refs) > 1:
            result.message = f"the {refs[0][0]!r} branch has no {params.get('path') or 'such file'}; used {linked}"
        return result
    raise AssertionError("unreachable")


def _archive(ctx: TaskContext, url: str, subpath: str | None, default_stem: str) -> HandlerResult:
    stem = subpath.rstrip("/").rsplit("/", 1)[-1] if subpath else default_stem
    if not subpath:
        return HandlerResult([download(url, lambda _: ctx.claim(stem), ext=".zip", expect="zip",
                                       progress=ctx.progress, cancel=ctx.cancel)])
    # A folder inside a repo: fetch the whole archive, keep only that folder.
    scratch = ctx.tmp_dir / f"{ctx.task.task_id}-archive"
    whole = download(url, lambda _: scratch, ext=".zip", expect="zip", progress=ctx.progress, cancel=ctx.cancel)
    try:
        base = ctx.claim(stem)
        dest = base.with_name(base.name + ".zip")
        _rezip_subfolder(whole, subpath, dest)
    finally:
        whole.unlink(missing_ok=True)
    return HandlerResult([dest])


def _rezip_subfolder(archive: Path, subpath: str, dest: Path) -> None:
    with zipfile.ZipFile(archive) as src:
        names = src.namelist()
        if not names:
            raise DownloadError(ErrorKind.NOT_FOUND, "empty archive")
        root = names[0].split("/", 1)[0]
        prefix = f"{root}/{subpath.strip('/')}/"
        members = [n for n in names if n.startswith(prefix) and not n.endswith("/")]
        if not members:
            raise DownloadError(ErrorKind.NOT_FOUND, f"folder {subpath!r} is not in the repository")
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as out:
            for name in members:
                with src.open(name) as fin, out.open(name[len(prefix):], "w") as fout:
                    shutil.copyfileobj(fin, fout)
        part.replace(dest)


def _gist(ctx: TaskContext, gist_id: str) -> HandlerResult:
    api = f"https://api.github.com/gists/{gist_id}"
    resp = session().get(api, timeout=REQUEST_TIMEOUT, headers={"Accept": "application/vnd.github+json"})
    if resp.status_code >= 400:
        raise error_for_status(resp.status_code, api)
    data = resp.json()
    folder = ctx.claim(data.get("description") or f"gist-{gist_id}")
    files = []
    for name, meta in (data.get("files") or {}).items():
        ctx.check_cancelled()
        stem, dot, ext = name.rpartition(".")
        target = folder / (stem if dot else name)
        files.append(download(meta["raw_url"], lambda _, t=target: t, ext=f".{ext}" if dot else ".txt",
                              progress=ctx.progress, cancel=ctx.cancel))
    return HandlerResult(files)
