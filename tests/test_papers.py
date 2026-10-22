from __future__ import annotations

import pytest

from scraper.engine import papers
from scraper.engine.errors import DownloadError
from scraper.identify import identify
from scraper.state import ErrorKind


def _fake_lookups(monkeypatch, s2, openalex, arxiv_title_hit=None):
    monkeypatch.setattr(papers, "_get_json", lambda url, **_: s2 if "semanticscholar" in url else openalex)
    monkeypatch.setattr(papers, "_arxiv_by_title", lambda title: arxiv_title_hit)


def test_arxiv_twin_is_preferred(monkeypatch):
    _fake_lookups(monkeypatch,
                  s2={"externalIds": {"ArXiv": "1512.03385"}, "openAccessPdf": {"url": "https://mirror.example/x.pdf"}},
                  openalex={})
    assert papers.open_access_pdf("10.1109/cvpr.2016.90") == "https://arxiv.org/pdf/1512.03385"


def test_arxiv_title_search_used_when_indexes_lack_arxiv(monkeypatch):
    _fake_lookups(monkeypatch, s2={}, arxiv_title_hit="1512.03385", openalex={
        "title": "Deep Residual Learning for Image Recognition",
        "best_oa_location": {"pdf_url": "https://mirror.example/resnet.pdf"},
    })
    assert papers.open_access_pdf("10.1109/cvpr.2016.90") == "https://arxiv.org/pdf/1512.03385"


ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>http://arxiv.org/abs/2001.99999v2</id><title>Deep Residual Learning: A Survey</title></entry>
<entry><id>http://arxiv.org/abs/1512.03385v1</id><title>Deep Residual Learning for
  Image Recognition</title></entry></feed>"""


def test_arxiv_title_match_must_be_exact(monkeypatch):
    class Resp:
        content = ATOM

        def raise_for_status(self):
            pass

    class Session:
        def get(self, *_args, **_kwargs):
            return Resp()

    monkeypatch.setattr(papers, "session", lambda: Session())
    assert papers._arxiv_by_title("Deep residual learning for image recognition.") == "1512.03385"
    assert papers._arxiv_by_title("Something else entirely") is None


def test_openalex_arxiv_location_beats_other_pdfs(monkeypatch):
    _fake_lookups(monkeypatch, s2={"openAccessPdf": {"url": ""}}, openalex={
        "best_oa_location": {"pdf_url": "https://mirror.example/x.pdf"},
        "locations": [{"landing_page_url": "https://arxiv.org/abs/1706.03762", "pdf_url": None},
                      {"landing_page_url": "https://mirror.example", "pdf_url": "https://mirror.example/x.pdf"}],
    })
    assert papers.open_access_pdf("10.65215/2q58a426") == "https://arxiv.org/pdf/1706.03762"


def test_falls_back_to_any_open_pdf(monkeypatch):
    _fake_lookups(monkeypatch, s2={"openAccessPdf": {"url": "https://repo.uni.edu/paper.pdf"}}, openalex={})
    assert papers.open_access_pdf("10.1/x") == "https://repo.uni.edu/paper.pdf"


def test_closed_paper_is_reported_paywalled(monkeypatch):
    _fake_lookups(monkeypatch, s2={"openAccessPdf": {"url": "", "status": "CLOSED"}},
                  openalex={"open_access": {"is_oa": False}, "locations": []})
    with pytest.raises(DownloadError) as err:
        papers.open_access_pdf("10.1145/3290605.3300233")
    assert err.value.kind == ErrorKind.PAYWALLED


def test_arxiv_doi_is_recognised_as_arxiv():
    ref = identify("https://doi.org/10.48550/arXiv.1706.03762")
    assert ref.provider == "arxiv" and ref.download_url == "https://arxiv.org/pdf/1706.03762"


@pytest.mark.network
def test_live_doi_resolution():
    assert papers.open_access_pdf("10.1109/CVPR.2016.90") == "https://arxiv.org/pdf/1512.03385"
    with pytest.raises(DownloadError) as err:
        papers.open_access_pdf("10.1145/3290605.3300233")
    assert err.value.kind == ErrorKind.PAYWALLED
