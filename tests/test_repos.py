"""Term-aware GitHub downloads.

themodernsoftware.dev's Fall 2025 page links its assignments on the repo's `master` branch,
which by October 2026 holds the Fall 2026 versions; Fall 2025 lives on a `fall2025` branch.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from scraper.engine import repos
from scraper.engine.context import TaskContext
from scraper.engine.errors import DownloadError
from scraper.identify import identify
from scraper.state import DownloadTask, ErrorKind

REPO = "https://github.com/mihail911/modern-software-dev-assignments"


@pytest.mark.parametrize(("names", "term", "ref"), [
    (["fall2025", "master"], "Fall 2025", "fall2025"),
    (["main", "fa25"], "Fall 2025", "fa25"),
    (["main", "2025-fall"], "fall 2025", "2025-fall"),
    (["main", "autumn-2025"], "Fall 2025", "autumn-2025"),
    (["main", "fall2025-solutions", "fall-2025"], "Fall 2025", "fall-2025"),  # exact beats "contains"
    (["main", "release-spring2024"], "Spring 2024", "release-spring2024"),
    (["master", "fall2026", "spring2025"], "Fall 2025", None),
    (["master", "conf25"], "Fall 2025", None),  # short tokens only count as exact names
    (["fall2025"], "not a term", None),
])
def test_matching_ref(names, term, ref):
    assert repos.matching_ref(names, term) == ref


def _ctx(url: str, term: str, tmp_path: Path) -> TaskContext:
    task = DownloadTask(url=url, term=term)
    return TaskContext(task=task, ref=identify(url), cancel=threading.Event(), tmp_dir=tmp_path,
                       _allocate=lambda folder, stem, key: tmp_path / stem, _report=lambda *_: None)


@pytest.fixture
def fake_github(monkeypatch):
    """Branches fall2025/master; records which URLs were downloaded; `missing` URLs 404."""
    fetched, missing = [], set()
    monkeypatch.setattr(repos, "_github_refs", lambda owner, repo: {"fall2025": "heads", "master": "heads"})

    def fake_download(url, claim, **_):
        fetched.append(url)
        if url in missing:
            raise DownloadError(ErrorKind.NOT_FOUND, "HTTP 404: not found")
        dest = claim("x").with_suffix(".md")
        dest.write_text("assignment")
        return dest

    def fake_archive(ctx, url, subpath, default_stem):
        fetched.append(f"{url}#{subpath}")
        if f"{url}#{subpath}" in missing:
            raise DownloadError(ErrorKind.NOT_FOUND, "folder not in repository")
        return repos.HandlerResult([ctx.tmp_dir / f"{subpath}.zip"])

    monkeypatch.setattr(repos, "download", fake_download)
    monkeypatch.setattr(repos, "_archive", fake_archive)
    return fetched, missing


def test_a_master_link_is_fetched_from_the_terms_branch(tmp_path, fake_github):
    fetched, _ = fake_github
    result = repos.download_repo(_ctx(f"{REPO}/blob/master/week3/assignment.md", "Fall 2025", tmp_path))
    assert fetched == ["https://raw.githubusercontent.com/mihail911/modern-software-dev-assignments/"
                       "refs/heads/fall2025/week3/assignment.md"]
    assert result.message == "Fall 2025 version, from the 'fall2025' branch (the link points to master)"


def test_a_folder_missing_on_the_terms_branch_falls_back_to_the_link(tmp_path, fake_github):
    fetched, missing = fake_github
    missing.add(f"{REPO}/archive/refs/heads/fall2025.zip#week9")
    result = repos.download_repo(_ctx(f"{REPO}/tree/master/week9", "Fall 2025", tmp_path))
    assert fetched == [f"{REPO}/archive/refs/heads/fall2025.zip#week9", f"{REPO}/archive/master.zip#week9"]
    assert result.message == "the 'fall2025' branch has no week9; used master"


def test_explicit_refs_and_runs_without_a_term_are_left_alone(tmp_path, fake_github):
    fetched, _ = fake_github
    repos.download_repo(_ctx(f"{REPO}/tree/v1.2/week1", "Fall 2025", tmp_path))
    repos.download_repo(_ctx(f"{REPO}/tree/master/week1", "", tmp_path))
    assert fetched == [f"{REPO}/archive/v1.2.zip#week1", f"{REPO}/archive/master.zip#week1"]


@pytest.mark.network
def test_live_fall2025_branch_is_found():
    repos._ref_cache.clear()
    refs = repos._github_refs("mihail911", "modern-software-dev-assignments")
    assert repos.matching_ref(list(refs), "Fall 2025") == "fall2025"
    assert refs["fall2025"] == "heads"  # a branch and a tag share the name; the branch wins
