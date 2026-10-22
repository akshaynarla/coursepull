from __future__ import annotations

import pytest

from scraper.identify import identify
from scraper.state import ResourceKind as K

PUB = "2PACX-1vQvvAavICHMLUbWmsf61UA_nmfbL67G0fTT9Ea6D7LONYcxQzKsnLhG2leydXOloXljHEtCNfVlAtr5"


@pytest.mark.parametrize(
    ("url", "kind", "provider", "resource_id"),
    [
        # Google: the published (/d/e/) forms used to be parsed with id "e"
        ("https://docs.google.com/presentation/d/11CP26VhsjnZOmi9YFgLlonzdib9BLyAlgc4cEvC5Fps/edit?usp=sharing",
         K.GOOGLE_SLIDES, "google", "11CP26VhsjnZOmi9YFgLlonzdib9BLyAlgc4cEvC5Fps"),
        (f"https://docs.google.com/presentation/d/e/{PUB}/embed?start=false", K.GOOGLE_SLIDES, "google", PUB),
        (f"https://docs.google.com/presentation/d/e/{PUB}/pub", K.GOOGLE_SLIDES, "google", PUB),
        ("https://docs.google.com/spreadsheets/d/e/2PACX-1vSabc123/pubhtml", K.GOOGLE_SHEET, "google", "2PACX-1vSabc123"),
        ("https://docs.google.com/document/d/1W11sWBigWIsi9wVIhhFx3wajIEnP0Qfb-ei8Ecf5VKU/edit",
         K.GOOGLE_DOC, "google", "1W11sWBigWIsi9wVIhhFx3wajIEnP0Qfb-ei8Ecf5VKU"),
        ("https://docs.google.com/a/uni.edu/presentation/d/1AbCdEfGhIjK/edit", K.GOOGLE_SLIDES, "google", "1AbCdEfGhIjK"),
        ("https://docs.google.com/presentation/u/1/d/1AbCdEfGhIjK/edit", K.GOOGLE_SLIDES, "google", "1AbCdEfGhIjK"),
        ("https://drive.google.com/file/d/11WnEbMGc9kny_WBpMN10I8oP8XsiQOnM/view?usp=sharing",
         K.DRIVE_FILE, "google", "11WnEbMGc9kny_WBpMN10I8oP8XsiQOnM"),
        ("https://drive.google.com/open?id=1AbCdEfGhIjK", K.DRIVE_FILE, "google", "1AbCdEfGhIjK"),
        ("https://drive.google.com/uc?id=1AbCdEfGhIjK&export=download", K.DRIVE_FILE, "google", "1AbCdEfGhIjK"),
        ("https://drive.google.com/drive/folders/19POzOxjeQ5b5b_8HHAfTb5-mqYqF-Heh",
         K.DRIVE_FOLDER, "google", "19POzOxjeQ5b5b_8HHAfTb5-mqYqF-Heh"),
        ("https://drive.google.com/drive/u/0/folders/1AbCdEfGhIjK?usp=sharing", K.DRIVE_FOLDER, "google", "1AbCdEfGhIjK"),
        ("https://colab.research.google.com/drive/1AbCdEfGhIjK", K.DRIVE_FILE, "google", "1AbCdEfGhIjK"),
        ("https://colab.research.google.com/github/owner/repo/blob/main/nb/intro.ipynb",
         K.REPO, "github", "owner/repo@main:nb/intro.ipynb"),
        # video
        ("https://www.youtube.com/watch?v=aG8NPTPhwkE&t=10", K.VIDEO, "youtube", "aG8NPTPhwkE"),
        ("https://youtu.be/aG8NPTPhwkE", K.VIDEO, "youtube", "aG8NPTPhwkE"),
        ("https://www.youtube-nocookie.com/embed/aG8NPTPhwkE?rel=0", K.VIDEO, "youtube", "aG8NPTPhwkE"),
        ("https://www.youtube.com/shorts/aG8NPTPhwkE", K.VIDEO, "youtube", "aG8NPTPhwkE"),
        ("https://www.youtube.com/playlist?list=PLabc", K.VIDEO, "youtube", "list:PLabc"),
        ("https://vimeo.com/123456789", K.VIDEO, "vimeo", None),
        # papers
        ("https://arxiv.org/abs/2101.00001v2", K.PAPER, "arxiv", "2101.00001v2"),
        ("https://arxiv.org/pdf/2101.00001.pdf", K.PAPER, "arxiv", "2101.00001"),
        ("https://arxiv.org/abs/hep-th/9901001", K.PAPER, "arxiv", "hep-th/9901001"),
        ("https://openreview.net/forum?id=BZ5a1r-kVsf", K.PAPER, "openreview", "BZ5a1r-kVsf"),
        ("https://aclanthology.org/2023.acl-long.1/", K.PAPER, "acl", "2023.acl-long.1"),
        ("https://proceedings.mlr.press/v202/kim23a.html", K.PAPER, "pmlr", "v202/kim23a"),
        ("https://doi.org/10.1145/3290605.3300233", K.PAPER, "doi", "10.1145/3290605.3300233"),
        ("https://dl.acm.org/doi/abs/10.1145/3290605.3300233", K.PAPER, "doi", "10.1145/3290605.3300233"),
        # code
        ("https://github.com/mees-robot-learning-course/ethz-course-2026", K.REPO, "github",
         "mees-robot-learning-course/ethz-course-2026"),
        ("https://github.com/o/r/tree/main/hw1_pytorch_tutorial", K.REPO, "github", "o/r@main:hw1_pytorch_tutorial"),
        ("https://github.com/o/r/blob/main/README.md", K.REPO, "github", "o/r@main:README.md"),
        ("https://gitlab.com/group/sub/project/-/tree/dev/src", K.REPO, "gitlab", "group/sub/project@dev:src"),
        # files and pages
        ("https://people.csail.mit.edu/brooks/papers/representation.pdf", K.FILE, "direct", None),
        ("https://example.edu/course/hw1.ipynb", K.FILE, "direct", None),
        ("https://example.edu/lectures/l01.mp4", K.FILE, "direct", None),
        ("https://www.alexirpan.com/2018/02/14/rl-hard.html", K.WEBPAGE, "generic", None),
        ("https://twitter.com/mapo1", K.WEBPAGE, "social", None),
        ("https://github.com/cvg", K.WEBPAGE, "github", None),
        ("https://canvas.instructure.com/courses/123", K.WEBPAGE, "lms", None),
    ],
)
def test_identify(url, kind, provider, resource_id):
    ref = identify(url)
    assert ref.kind == kind
    assert ref.provider == provider
    if resource_id is not None:
        assert ref.resource_id == resource_id


def test_published_google_links_are_flagged():
    assert identify(f"https://docs.google.com/presentation/d/e/{PUB}/pub").published
    assert not identify("https://docs.google.com/presentation/d/11CP26VhsjnZOmi9YFgLlonzdib9/edit").published


@pytest.mark.parametrize(
    ("url", "pdf"),
    [
        ("https://arxiv.org/abs/2101.00001v2", "https://arxiv.org/pdf/2101.00001v2"),
        ("https://openreview.net/forum?id=abc", "https://openreview.net/pdf?id=abc"),
        ("https://aclanthology.org/P19-1001", "https://aclanthology.org/P19-1001.pdf"),
        ("https://proceedings.mlr.press/v202/kim23a.html", "https://proceedings.mlr.press/v202/kim23a/kim23a.pdf"),
        ("https://openaccess.thecvf.com/content/CVPR2023/html/Kim_X_CVPR_2023_paper.html",
         "https://openaccess.thecvf.com/content/CVPR2023/papers/Kim_X_CVPR_2023_paper.pdf"),
        ("https://papers.nips.cc/paper/2017/hash/3f5ee243547dee91fbd053c1c4a845aa-Abstract.html",
         "https://proceedings.neurips.cc/paper_files/paper/2017/file/3f5ee243547dee91fbd053c1c4a845aa-Paper.pdf"),
        ("https://proceedings.neurips.cc/paper_files/paper/2022/hash/abc123-Abstract-Conference.html",
         "https://proceedings.neurips.cc/paper_files/paper/2022/file/abc123-Paper-Conference.pdf"),
        ("https://github.com/o/r/blob/main/docs/a.pdf", "https://raw.githubusercontent.com/o/r/main/docs/a.pdf"),
    ],
)
def test_download_urls(url, pdf):
    assert identify(url).download_url == pdf


def test_same_resource_shares_key():
    a = identify("https://docs.google.com/presentation/d/1AbCdEfGhIjK/edit?usp=sharing")
    b = identify("https://docs.google.com/presentation/d/1AbCdEfGhIjK/view")
    c = identify("https://drive.google.com/open?id=1AbCdEfGhIjK")
    assert a.key == b.key == c.key
    assert identify("https://youtu.be/aG8NPTPhwkE").key == identify("https://www.youtube.com/watch?v=aG8NPTPhwkE").key
