"""Open-access papers: arxiv, OpenReview, ACL, PMLR, CVF, NeurIPS, bioRxiv, DOIs.

DOIs are resolved through open scholarly indexes (OpenAlex, arXiv's own search,
Semantic Scholar) that need no key and no email. An arXiv copy is preferred
whenever one exists.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests

from ..config import REQUEST_TIMEOUT
from ..state import ErrorKind
from .context import HandlerResult, TaskContext
from .errors import DownloadError
from .http import download, session

log = logging.getLogger(__name__)


def download_paper(ctx: TaskContext) -> HandlerResult:
    ref = ctx.ref
    if ref.provider == "doi":
        url = open_access_pdf(ref.resource_id or "")
    elif ref.download_url:
        url = ref.download_url
    else:
        kind = ErrorKind.PAYWALLED if ref.login_likely else ErrorKind.UNSUPPORTED
        raise DownloadError(kind, ref.hint or "no known PDF location for this paper page")
    return HandlerResult([download(url, ctx.claim, ext=".pdf", expect="pdf",
                                   progress=ctx.progress, cancel=ctx.cancel)])


def open_access_pdf(doi: str) -> str:
    """A legal open-access PDF URL for a DOI, preferring arXiv.

    OpenAlex first (reliable, keyless; also gives the title), then an exact-title
    search on arXiv, then Semantic Scholar, whose keyless tier is often rate limited.
    """
    candidates: list[str] = []
    title = None
    try:
        arxiv_id, pdfs, title = _openalex(doi)
        if arxiv_id:
            return f"https://arxiv.org/pdf/{arxiv_id}"
        candidates.extend(pdfs)
    except (requests.RequestException, ValueError) as exc:
        log.info("OpenAlex lookup failed for %s: %s", doi, exc)

    if title:
        try:
            if arxiv_id := _arxiv_by_title(title):
                return f"https://arxiv.org/pdf/{arxiv_id}"
        except (requests.RequestException, ET.ParseError) as exc:
            log.info("arXiv title search failed for %r: %s", title, exc)

    try:
        arxiv_id, pdfs = _semantic_scholar(doi)
        if arxiv_id:
            return f"https://arxiv.org/pdf/{arxiv_id}"
        candidates.extend(p for p in pdfs if p not in candidates)
    except (requests.RequestException, ValueError) as exc:
        log.info("Semantic Scholar lookup failed for %s: %s", doi, exc)

    if candidates:
        return candidates[0]
    raise DownloadError(ErrorKind.PAYWALLED, f"no open-access copy of {doi} is known")


def _get_json(url: str, **params) -> dict:
    resp = session().get(url, params=params or None, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()
    return resp.json()


def _semantic_scholar(doi: str) -> tuple[str | None, list[str]]:
    data = _get_json(f"https://api.semanticscholar.org/graph/v1/paper/DOI:{quote(doi, safe='/')}",
                     fields="externalIds,openAccessPdf")
    arxiv_id = (data.get("externalIds") or {}).get("ArXiv")
    pdf = (data.get("openAccessPdf") or {}).get("url")
    return arxiv_id, [pdf] if pdf else []


def _openalex(doi: str) -> tuple[str | None, list[str], str | None]:
    data = _get_json(f"https://api.openalex.org/works/doi:{quote(doi, safe='/')}")
    arxiv_id, pdfs = None, []
    for location in data.get("locations") or []:
        landing = location.get("landing_page_url") or ""
        if "arxiv.org/abs/" in landing:
            arxiv_id = landing.split("arxiv.org/abs/", 1)[1].strip("/")
        elif location.get("pdf_url"):
            pdfs.append(location["pdf_url"])
    best = (data.get("best_oa_location") or {}).get("pdf_url")
    if best and best not in pdfs:
        pdfs.insert(0, best)
    return arxiv_id, pdfs, data.get("title")


_ATOM = {"a": "http://www.w3.org/2005/Atom"}


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _arxiv_by_title(title: str) -> str | None:
    """arXiv id of the paper with exactly this title, if arXiv has one."""
    phrase = re.sub(r'["\\]', " ", title)
    resp = session().get("https://export.arxiv.org/api/query", timeout=REQUEST_TIMEOUT,
                         params={"search_query": f'ti:"{phrase}"', "max_results": 5})
    resp.raise_for_status()
    wanted = _norm_title(title)
    for entry in ET.fromstring(resp.content).findall("a:entry", _ATOM):
        found = entry.findtext("a:title", default="", namespaces=_ATOM)
        if _norm_title(found) == wanted:
            entry_id = entry.findtext("a:id", default="", namespaces=_ATOM)
            return re.sub(r"v\d+$", "", entry_id.rsplit("/abs/", 1)[-1])
    return None
