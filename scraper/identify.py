"""Recognise what a URL points at, without touching the network.

Agents use this as a tool: it turns a raw link into a typed ResourceRef so they
can reason about "a Google Slides deck" or "an arxiv paper" instead of URL
syntax. It never decides whether a resource is needed for the course.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import PurePosixPath
from urllib.parse import parse_qs, unquote, urlparse

from .state import ResourceKind as K
from .state import ResourceRef

# ---------- helpers ----------


def _host(netloc: str) -> str:
    host = netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _host_is(host: str, *domains: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _query(parsed) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(parsed.query).items() if v}


# ---------- Google Docs / Slides / Sheets / Drive / Colab ----------

_GDOC_PATH = re.compile(r"^/(document|presentation|spreadsheets)/d/(?:(e)/)?([A-Za-z0-9_-]+)")
_GDOC_KINDS = {"document": K.GOOGLE_DOC, "presentation": K.GOOGLE_SLIDES, "spreadsheets": K.GOOGLE_SHEET}
_DRIVE_FILE_PATH = re.compile(r"^/file/d/([A-Za-z0-9_-]+)")
_DRIVE_FOLDER_PATH = re.compile(r"^/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]+)")
_COLAB_DRIVE = re.compile(r"^/drive/([A-Za-z0-9_-]+)")
_COLAB_GITHUB = re.compile(r"^/github/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$")


def _google(parsed, host: str, url: str) -> ResourceRef | None:
    # Workspace-domain (/a/<domain>/) and multi-account (/u/<n>/) prefixes don't change the file.
    path = re.sub(r"^/a/[^/]+", "", parsed.path)
    path = re.sub(r"/u/\d+(?=/)", "", path)
    q = _query(parsed)

    if host == "docs.google.com":
        if path.startswith(("/viewer", "/viewerng")) and q.get("url"):
            return identify(q["url"])  # Google's viewer wrapped around another file
        if path.startswith("/forms/"):
            return ResourceRef(url=url, kind=K.WEBPAGE, provider="google_forms",
                               hint="Google Form; nothing to download")
        if m := _GDOC_PATH.match(path):
            kind = _GDOC_KINDS[m.group(1)]
            published = m.group(2) == "e"
            return ResourceRef(
                url=url, kind=kind, provider="google", resource_id=m.group(3), published=published,
                hint="published to the web: no export endpoint, rendered instead" if published else "",
            )
        return None

    if host == "drive.google.com":
        if m := _DRIVE_FILE_PATH.match(path):
            return ResourceRef(url=url, kind=K.DRIVE_FILE, provider="google", resource_id=m.group(1))
        if m := _DRIVE_FOLDER_PATH.match(path):
            return ResourceRef(url=url, kind=K.DRIVE_FOLDER, provider="google", resource_id=m.group(1))
        if path.startswith(("/folderview", "/embeddedfolderview")) and q.get("id"):
            return ResourceRef(url=url, kind=K.DRIVE_FOLDER, provider="google", resource_id=q["id"])
        if path.startswith(("/open", "/uc")) and q.get("id"):
            return ResourceRef(url=url, kind=K.DRIVE_FILE, provider="google", resource_id=q["id"],
                               params={"maybe_folder": "1"} if path.startswith("/open") else {},
                               hint="an /open?id= link can also be a folder" if path.startswith("/open") else "")
        return ResourceRef(url=url, kind=K.WEBPAGE, provider="google", login_likely=True,
                           hint="Drive page that is neither a file nor a folder")

    if host == "drive.usercontent.google.com" and q.get("id"):
        return ResourceRef(url=url, kind=K.DRIVE_FILE, provider="google", resource_id=q["id"])

    if host == "colab.research.google.com":
        if m := _COLAB_DRIVE.match(path):
            return ResourceRef(url=url, kind=K.DRIVE_FILE, provider="google", resource_id=m.group(1),
                               hint="Colab notebook stored in Drive")
        if m := _COLAB_GITHUB.match(path):
            owner, repo, ref, sub = m.groups()
            return _github_ref(url, owner, repo, ref, sub, blob=True, hint="Colab notebook from GitHub")
    return None


# ---------- video ----------

_YT_ID = r"[A-Za-z0-9_-]{11}"
_YT_PATH_ID = re.compile(rf"^/(?:embed|shorts|live|v|e)/({_YT_ID})")


def _youtube(parsed, host: str, url: str) -> ResourceRef | None:
    if not (_host_is(host, "youtube.com", "youtube-nocookie.com") or host == "youtu.be"):
        return None
    q = _query(parsed)
    video_id = None
    if host == "youtu.be":
        video_id = parsed.path.strip("/")[:11] or None
    elif parsed.path == "/watch":
        video_id = q.get("v")
    elif m := _YT_PATH_ID.match(parsed.path):
        video_id = m.group(1)
    if video_id:
        hint = "part of a playlist" if q.get("list") else ""
        return ResourceRef(url=url, kind=K.VIDEO, provider="youtube", resource_id=video_id, hint=hint)
    if parsed.path == "/playlist" and q.get("list"):
        return ResourceRef(url=url, kind=K.VIDEO, provider="youtube", resource_id=f"list:{q['list']}",
                           params={"playlist": "1"}, hint="playlist")
    return ResourceRef(url=url, kind=K.WEBPAGE, provider="youtube",
                       hint="YouTube channel or page, not a single video")


# Video hosts that usually sit behind an institutional login.
_LOGIN_VIDEO_HOSTS = ("panopto.com", "panopto.eu", "kaltura.com", "video.ethz.ch", "mediasite.com")


def _ytdlp_extractor(url: str) -> str | None:
    """Name of a yt-dlp extractor (other than the generic one) that claims this URL."""
    for ie in _ytdlp_extractors():
        if ie.suitable(url):
            return ie.ie_key()
    return None


@lru_cache(maxsize=1)
def _ytdlp_extractors() -> tuple:
    from yt_dlp.extractor import gen_extractor_classes

    return tuple(ie for ie in gen_extractor_classes() if ie.ie_key() != "Generic" and ie._VALID_URL)


# ---------- papers ----------

_ARXIV = re.compile(
    r"^/(?:abs|pdf|html)/((?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?)(?:\.pdf)?/?$"
)
_ACL_ID = re.compile(r"^/((?:[A-Z]\d{2}-\d{4})|(?:\d{4}\.[a-z0-9-]+\.\d+))(?:\.pdf)?/?$", re.IGNORECASE)
_PMLR = re.compile(r"^/(v\d+)/([^/]+?)(?:\.html|/\2\.pdf)$")
_NEURIPS = re.compile(r"/paper(?:_files/paper)?/(\d{4})/(?:hash|file)/([0-9a-f]+)-(?:Abstract|Paper)(-[A-Za-z_]+)?\.(?:html|pdf)$")
_BIORXIV = re.compile(r"^/content/(10\.\d{4,9}/[^v]+?)(v\d+)?(?:\.full)?(?:\.pdf)?/?$")
_DOI_IN_PATH = re.compile(r"(10\.\d{4,9}/[^?#\s]+)")
_DOI_PUBLISHERS = (
    "dl.acm.org", "link.springer.com", "onlinelibrary.wiley.com", "tandfonline.com",
    "journals.sagepub.com", "pubs.acs.org", "journals.aps.org", "iopscience.iop.org", "academic.oup.com",
)
_PAYWALL_HOSTS = ("ieeexplore.ieee.org", "sciencedirect.com", "jstor.org")


def _paper(parsed, host: str, url: str) -> ResourceRef | None:
    path = unquote(parsed.path)
    q = _query(parsed)

    if _host_is(host, "arxiv.org") and (m := _ARXIV.match(path)):
        aid = m.group(1)
        return ResourceRef(url=url, kind=K.PAPER, provider="arxiv", resource_id=aid,
                           download_url=f"https://arxiv.org/pdf/{aid}")
    if host == "openreview.net" and path in ("/forum", "/pdf") and q.get("id"):
        return ResourceRef(url=url, kind=K.PAPER, provider="openreview", resource_id=q["id"],
                           download_url=f"https://openreview.net/pdf?id={q['id']}")
    if host == "aclanthology.org" and (m := _ACL_ID.match(path)):
        return ResourceRef(url=url, kind=K.PAPER, provider="acl", resource_id=m.group(1),
                           download_url=f"https://aclanthology.org/{m.group(1)}.pdf")
    if host == "proceedings.mlr.press" and (m := _PMLR.match(path)):
        vol, key = m.groups()
        return ResourceRef(url=url, kind=K.PAPER, provider="pmlr", resource_id=f"{vol}/{key}",
                           download_url=f"https://proceedings.mlr.press/{vol}/{key}/{key}.pdf")
    if host == "openaccess.thecvf.com" and "/html/" in path and path.endswith(".html"):
        pdf_path = path.replace("/html/", "/papers/")[: -len(".html")] + ".pdf"
        return ResourceRef(url=url, kind=K.PAPER, provider="cvf", resource_id=pdf_path,
                           download_url=f"https://openaccess.thecvf.com{pdf_path}")
    if host in ("papers.nips.cc", "proceedings.neurips.cc") and (m := _NEURIPS.search(path)):
        year, digest, suffix = m.group(1), m.group(2), m.group(3) or ""
        return ResourceRef(
            url=url, kind=K.PAPER, provider="neurips", resource_id=digest,
            download_url=f"https://proceedings.neurips.cc/paper_files/paper/{year}/file/{digest}-Paper{suffix}.pdf",
        )
    if host in ("biorxiv.org", "medrxiv.org") and (m := _BIORXIV.match(path)):
        doi, version = m.group(1), m.group(2) or ""
        return ResourceRef(url=url, kind=K.PAPER, provider=host.split(".")[0], resource_id=doi,
                           download_url=f"https://www.{host}/content/{doi}{version}.full.pdf")
    if host in ("doi.org", "dx.doi.org") and (m := _DOI_IN_PATH.search(path)):
        return _doi_ref(url, m.group(1))
    if _host_is(host, *_DOI_PUBLISHERS) and (m := _DOI_IN_PATH.search(path)):
        return _doi_ref(url, m.group(1), hint=f"publisher page on {host}")
    if host == "nature.com" and path.startswith("/articles/"):
        return _doi_ref(url, "10.1038/" + path.split("/")[2], hint="Nature article")
    if _host_is(host, *_PAYWALL_HOSTS):
        return ResourceRef(url=url, kind=K.PAPER, provider=host, login_likely=True,
                           hint="publisher page, usually paywalled; look for an arxiv or open-access copy")
    return None


def _doi_ref(url: str, doi: str, hint: str = "") -> ResourceRef:
    doi = doi.rstrip("/.")
    if doi.lower().startswith("10.48550/arxiv."):  # arXiv's own DOIs
        aid = doi[len("10.48550/arxiv."):]
        return ResourceRef(url=url, kind=K.PAPER, provider="arxiv", resource_id=aid,
                           download_url=f"https://arxiv.org/pdf/{aid}")
    return ResourceRef(url=url, kind=K.PAPER, provider="doi", resource_id=doi.lower(),
                       hint=hint or "DOI: resolved to an open-access copy (arXiv preferred) if one exists")


# ---------- code repositories ----------

_GITHUB = re.compile(r"^/([^/]+)/([^/]+?)(?:\.git)?(?:/(tree|blob|raw)/([^/]+)(?:/(.*))?)?/?$")
_GITHUB_RESERVED = {"orgs", "topics", "features", "marketplace", "sponsors", "settings", "login", "about"}
_GITLAB = re.compile(r"^/(.+?)(?:/-/(tree|blob|raw)/([^/]+)(?:/(.*))?)?/?$")


def _github_ref(url: str, owner: str, repo: str, ref: str | None, sub: str | None,
                blob: bool, hint: str = "") -> ResourceRef:
    sub = (sub or "").strip("/")
    if blob and ref and sub:
        return ResourceRef(
            url=url, kind=K.REPO, provider="github", resource_id=f"{owner}/{repo}@{ref}:{sub}",
            download_url=f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{sub}",
            params={"owner": owner, "repo": repo, "ref": ref, "path": sub, "blob": "1"},
            hint=hint or "single file in a GitHub repo",
        )
    params = {"owner": owner, "repo": repo}
    if ref:
        params["ref"] = ref
    if sub:
        params["path"] = sub
    rid = f"{owner}/{repo}" + (f"@{ref}" if ref else "") + (f":{sub}" if sub else "")
    return ResourceRef(url=url, kind=K.REPO, provider="github", resource_id=rid, params=params,
                       hint=hint or ("folder in a GitHub repo" if sub else "GitHub repository"))


def _repo(parsed, host: str, url: str) -> ResourceRef | None:
    path = unquote(parsed.path)
    if host == "github.com":
        m = _GITHUB.match(path)
        if not m or m.group(1) in _GITHUB_RESERVED:
            parts = [p for p in path.split("/") if p]
            if len(parts) >= 4 and parts[2] == "releases" and parts[3] == "download":
                return ResourceRef(url=url, kind=K.FILE, provider="github", hint="release asset")
            hint = "GitHub profile" if len(parts) == 1 else "GitHub page (issues, wiki, ...), not a repo"
            return ResourceRef(url=url, kind=K.WEBPAGE, provider="github", hint=hint)
        owner, repo, mode, ref, sub = m.groups()
        return _github_ref(url, owner, repo, ref, sub, blob=mode in ("blob", "raw"))
    if host == "raw.githubusercontent.com":
        return ResourceRef(url=url, kind=K.FILE, provider="github", download_url=url)
    if host == "gist.github.com":
        parts = [p for p in path.split("/") if p]
        if parts:
            gist_id = parts[-1]
            return ResourceRef(url=url, kind=K.REPO, provider="gist", resource_id=gist_id,
                               params={"gist": gist_id}, hint="GitHub gist")
    if host == "gitlab.com" and (m := _GITLAB.match(path)):
        project, mode, ref, sub = m.groups()
        if "/-/" in project:
            return ResourceRef(url=url, kind=K.WEBPAGE, provider="gitlab",
                               hint="GitLab page (issues, wiki, ...), not a repo")
        if project.count("/") < 1:
            return ResourceRef(url=url, kind=K.WEBPAGE, provider="gitlab", hint="GitLab group or profile")
        params = {"project": project}
        if ref:
            params["ref"] = ref
        if sub:
            params["path"] = sub.strip("/")
        if mode in ("blob", "raw") and ref and sub:
            params["blob"] = "1"
            return ResourceRef(url=url, kind=K.REPO, provider="gitlab", resource_id=f"{project}@{ref}:{sub}",
                               download_url=f"https://gitlab.com/{project}/-/raw/{ref}/{sub}", params=params,
                               hint="single file in a GitLab repo")
        rid = project + (f"@{ref}" if ref else "") + (f":{params['path']}" if "path" in params else "")
        return ResourceRef(url=url, kind=K.REPO, provider="gitlab", resource_id=rid, params=params,
                           hint="GitLab repository")
    return None


# ---------- sites that never hold downloadable course material ----------

_SOCIAL_HOSTS = (
    "twitter.com", "x.com", "linkedin.com", "facebook.com", "instagram.com", "threads.net",
    "bsky.app", "mastodon.social", "scholar.google.com", "researchgate.net", "orcid.org",
)
_LOGIN_HOSTS = (
    "instructure.com", "canvas.net", "blackboard.com", "brightspace.com", "desire2learn.com",
    "coursera.org", "edx.org", "piazza.com", "gradescope.com", "edstem.org", "campuswire.com",
    "zoom.us", "teams.microsoft.com", "sharepoint.com", "slack.com", "discord.com", "discord.gg",
)


def _site(parsed, host: str, url: str) -> ResourceRef | None:
    if _host_is(host, *_SOCIAL_HOSTS) or (host.startswith("google.") and parsed.path.startswith("/maps")):
        return ResourceRef(url=url, kind=K.WEBPAGE, provider="social", hint="social or profile page")
    if _host_is(host, *_LOGIN_HOSTS) or host.startswith(("moodle.", "canvas.", "lms.", "elearning.")):
        provider = "zoom" if _host_is(host, "zoom.us") else "lms"
        # Zoom recordings shared publicly are fine for yt-dlp; meeting links are not.
        if provider == "zoom" and "/rec/" in parsed.path:
            return ResourceRef(url=url, kind=K.VIDEO, provider="zoom", login_likely=True,
                               hint="Zoom recording; often needs a passcode or login")
        return ResourceRef(url=url, kind=K.WEBPAGE, provider=provider, login_likely=True,
                           hint="learning platform or meeting link; normally needs a login")
    return None


# ---------- plain files ----------

_DOC_EXT = {
    ".pdf", ".ps", ".eps", ".djvu", ".epub", ".mobi",
    ".ppt", ".pptx", ".pps", ".ppsx", ".key", ".odp",
    ".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".tex", ".bib",
    ".xls", ".xlsx", ".ods", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml",
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
    ".ipynb", ".py", ".r", ".m", ".jl", ".c", ".cc", ".cpp", ".h", ".hpp", ".java", ".js", ".ts",
    ".go", ".rs", ".sh", ".sql", ".scala", ".hs", ".ml", ".lisp", ".scm", ".rkt",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
}
_MEDIA_EXT = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".mp3", ".m4a", ".wav", ".ogg", ".flac"}


def _file(parsed, host: str, url: str) -> ResourceRef | None:
    ext = PurePosixPath(unquote(parsed.path)).suffix.lower()
    if ext in _MEDIA_EXT:
        return ResourceRef(url=url, kind=K.FILE, provider="direct", download_url=url, hint=f"{ext} media file")
    if ext in _DOC_EXT:
        return ResourceRef(url=url, kind=K.FILE, provider="direct", download_url=url, hint=f"{ext} file")
    return None


# ---------- entry point ----------


@lru_cache(maxsize=4096)
def identify(url: str) -> ResourceRef:
    """Classify a URL by shape alone. Cheap, deterministic and offline."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        return ResourceRef(url=url, kind=K.UNKNOWN, hint=f"unsupported scheme {parsed.scheme!r}")
    host = _host(parsed.netloc)

    for recogniser in (_google, _youtube, _paper, _repo, _site, _file):
        if ref := recogniser(parsed, host, url):
            return ref

    if _host_is(host, *_LOGIN_VIDEO_HOSTS):
        return ResourceRef(url=url, kind=K.VIDEO, provider=host, login_likely=True,
                           hint="institutional video platform; usually needs a login")
    if extractor := _ytdlp_extractor(url):
        return ResourceRef(url=url, kind=K.VIDEO, provider=extractor.lower(), hint=f"video ({extractor})")
    return ResourceRef(url=url, kind=K.WEBPAGE)
