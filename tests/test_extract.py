from __future__ import annotations

from scraper.extract import extract, extract_views, page_outline

PAGE = """
<html><head><title>Robot Learning</title><base href="https://course.edu/2026/"></head>
<body>
<h1>Robot Learning</h1>
<div role="tablist"><button type="button">Overview</button><button type="button">Syllabus</button>
<button>Sign in</button></div>
<form><button>Search</button></form>
<h2>Schedule</h2>
<table>
  <tr><td>Week 3</td><td>Policy Gradients</td>
      <td><a href="slides/l3.pdf">slides</a></td>
      <td><a href="https://youtu.be/aG8NPTPhwkE">video</a></td></tr>
</table>
<h3>Embedded</h3>
<iframe title="Lecture deck" src="https://docs.google.com/presentation/d/e/2PACX-1vQabc/embed?start=false"></iframe>
<iframe data-src="https://www.youtube.com/embed/aG8NPTPhwkE"></iframe>
<video controls><source src="/media/intro.mp4" type="video/mp4"></video>
<h2>Readings</h2>
<ul><li><a href="https://arxiv.org/abs/2101.00001">Paper A</a> (required)</li></ul>
<a href="&#109;&#97;&#105;&#108;&#116;&#111;&#58;x@y.ch">mail</a>
<a href="javascript:void(0)">js</a>
<a href="#top">top</a>
<a href="slides/l3.pdf#page=2">slides again</a>
</body></html>
"""


def _by_url(links):
    return {link.url: link for link in links}


def test_extracts_links_embeds_and_media():
    links = _by_url(extract(PAGE, "https://course.edu/index.html"))
    assert set(links) == {
        "https://course.edu/2026/slides/l3.pdf",
        "https://youtu.be/aG8NPTPhwkE",
        "https://docs.google.com/presentation/d/e/2PACX-1vQabc/embed?start=false",
        "https://www.youtube.com/embed/aG8NPTPhwkE",
        "https://course.edu/media/intro.mp4",
        "https://arxiv.org/abs/2101.00001",
    }
    assert links["https://docs.google.com/presentation/d/e/2PACX-1vQabc/embed?start=false"].source == "iframe"
    assert links["https://docs.google.com/presentation/d/e/2PACX-1vQabc/embed?start=false"].text == "Lecture deck"
    assert links["https://www.youtube.com/embed/aG8NPTPhwkE"].source == "iframe"
    assert links["https://course.edu/media/intro.mp4"].source == "video"


def test_context_path_and_row_snippet():
    links = _by_url(extract(PAGE, "https://course.edu/"))
    slides = links["https://course.edu/2026/slides/l3.pdf"]
    assert slides.context_path == "Robot Learning > Schedule"
    assert "Week 3" in slides.snippet and "Policy Gradients" in slides.snippet
    paper = links["https://arxiv.org/abs/2101.00001"]
    assert paper.context_path == "Robot Learning > Readings"
    assert "(required)" in paper.snippet
    embed = links["https://www.youtube.com/embed/aG8NPTPhwkE"]
    assert embed.context_path == "Robot Learning > Schedule > Embedded"


def test_views_label_links_revealed_by_a_control():
    before = '<html><body><a href="/a.pdf">a</a></body></html>'
    after = '<html><body><a href="/a.pdf">a</a><h2>Week 1</h2><a href="/b.pdf">b</a></body></html>'
    links = _by_url(extract_views([("", before), ("Syllabus", after)], "https://c.edu/"))
    assert links["https://c.edu/a.pdf"].context_path == ""
    assert links["https://c.edu/b.pdf"].context_path == "[Syllabus] Week 1"


def test_outline_lists_controls_that_may_hide_content():
    outline = page_outline(PAGE)
    assert outline.title == "Robot Learning"
    assert outline.controls == ["Overview", "Syllabus"]  # "Sign in" denied, form buttons ignored
    assert "## Schedule" in outline.headings
    assert "Policy Gradients" in outline.text_excerpt
