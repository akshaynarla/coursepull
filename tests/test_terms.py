"""Offering (term) facts, modelled on themodernsoftware.dev as of 2026-10-22: the homepage
shows Fall 2026 (in progress) and links "Fall 2025" -> /fall2025; /fall2025's title names
Fall 2025 and it links "Fall 2026" back to the homepage.

Run 3 sent its only Scout to the homepage, labelled it "Fall 2025", and never opened /fall2025.
"""

from __future__ import annotations

import pytest

from scraper.agents.loop import ToolError
from scraper.agents.terms import OfferingMap, check_term_choice, term_in
from scraper.models import Link

HOME, ARCHIVE = "https://themodernsoftware.dev/", "https://themodernsoftware.dev/fall2025"
HOME_LINKS = [Link(url=ARCHIVE, text="Fall 2025"), Link(url="https://docs.google.com/x", text="Calendar"),
              Link(url="https://other.edu/fall2024", text="Fall 2024")]  # another site: not an offering
ARCHIVE_LINKS = [Link(url=HOME, text="Fall 2026")]


@pytest.mark.parametrize(("text", "term"), [
    ("Fall 2025", "Fall 2025"), ("fall2025", "Fall 2025"), ("Spring-2024 offering", "Spring 2024"),
    ("CS146S - Fall 2025 - Stanford", "Fall 2025"), ("Fall 2025 and Fall 2026", None), ("CS146S", None),
])
def test_term_in(text, term):
    assert term_in(text) == term


def _homepage_only() -> OfferingMap:
    offerings = OfferingMap()
    offerings.learn(HOME, "CS146S: The Modern Software Developer - Stanford University", HOME_LINKS)
    return offerings


def _both_pages() -> OfferingMap:
    offerings = _homepage_only()
    offerings.learn(ARCHIVE, "CS146S: The Modern Software Developer - Fall 2025 - Stanford University",
                    ARCHIVE_LINKS)
    return offerings


def test_offerings_are_learned_from_titles_and_term_switcher_links():
    offerings = _both_pages()
    assert offerings.term_of(ARCHIVE) == "Fall 2025" and "title" in offerings.evidence[ARCHIVE.lower()]
    assert offerings.term_of(HOME) == "Fall 2026"
    assert offerings.term_of("https://other.edu/fall2024") is None


def test_the_homepage_listing_says_another_offering_is_linked():
    (line,) = _homepage_only().describe(HOME, HOME_LINKS)
    assert line.startswith(f"other offerings of this course linked here: Fall 2025 -> {ARCHIVE} "
                           "(not yet read with its tabs revealed)")
    assert "this page is probably another offering itself" in line


def test_run_3s_choice_is_refused_with_the_facts():
    # Term "Fall 2025", but the only Scout starts at the homepage.
    with pytest.raises(ToolError, match="Fall 2025 offering is at .*fall2025 .*no Scout starts there"):
        check_term_choice("Fall 2025", [HOME], HOME, _homepage_only())


def test_run_2s_missing_term_is_refused():
    with pytest.raises(ToolError, match="links other offerings .*Fall 2025.*set term"):
        check_term_choice("", [HOME, ARCHIVE], HOME, _homepage_only())


def test_a_scout_on_another_terms_page_is_refused():
    with pytest.raises(ToolError, match="https://themodernsoftware.dev/ is Fall 2026"):
        check_term_choice("Fall 2025", [ARCHIVE, HOME], HOME, _both_pages())


def test_the_right_choice_passes():
    check_term_choice("Fall 2025", [ARCHIVE], HOME, _both_pages())
    check_term_choice("", [HOME], HOME, OfferingMap())  # a site with one offering


# ---------- "most complete" is counted, not guessed (run 4 picked the smaller term) ----------


def _materials(n: int, prefix: str) -> list[Link]:
    return [Link(url=f"https://docs.google.com/presentation/d/{prefix}{i:04}abcdefgh/edit", text="slides")
            for i in range(n)]


def _real_sizes(read_archive: bool = True) -> OfferingMap:
    """Live counts on 2026-10-22, tabs revealed: Fall 2026 homepage 20, Fall 2025 archive 39."""
    offerings = OfferingMap()
    offerings.learn(HOME, "CS146S: The Modern Software Developer - Stanford University",
                    HOME_LINKS + _materials(20, "home"))
    if read_archive:
        offerings.learn(ARCHIVE, "CS146S: The Modern Software Developer - Fall 2025 - Stanford University",
                        ARCHIVE_LINKS + _materials(39, "arch"))
    return offerings


def test_run_4s_choice_of_the_smaller_term_is_refused():
    with pytest.raises(ToolError, match="Fall 2026 links 20 course materials, Fall 2025 links 39"):
        check_term_choice("Fall 2026", [HOME], HOME, _real_sizes())
    check_term_choice("Fall 2025", [ARCHIVE], HOME, _real_sizes())


def test_a_term_cannot_be_called_most_complete_before_it_is_read():
    with pytest.raises(ToolError, match="compare the offerings.*Fall 2025 not yet read.*reveal_page"):
        check_term_choice("Fall 2025", [ARCHIVE], HOME, _real_sizes(read_archive=False))


def test_a_partial_view_does_not_count_as_a_size():
    offerings = OfferingMap()
    offerings.learn(HOME, "Course", HOME_LINKS, complete=False)  # tabs not revealed yet
    assert offerings.sizes == {}


def test_listings_show_each_offerings_size():
    offerings = _real_sizes()
    own, linked = offerings.describe(HOME, HOME_LINKS)
    assert own.startswith("offering: this page is Fall 2026") and own.endswith("20 course-material links")
    assert linked == f"other offerings of this course linked here: Fall 2025 -> {ARCHIVE} (39 course-material links)"
