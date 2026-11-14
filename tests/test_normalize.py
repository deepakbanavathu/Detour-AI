"""Text normalization: what reaches the parser and the gazetteer."""
from app import gazetteer

ABBR = [(0, "jn", "junction"), (0, "x roads", "cross roads"), (0, "rd", "road"), (0, "u/p", "underpass"), (0, "twds", "towards"),
        (1, "pvnr", "pv narasimha rao expressway"), (1, "lb nagar", "l b nagar")]


def setup_module():
    gazetteer.set_abbreviations(ABBR)


def test_strips_urls_mentions_hashtags_and_emoji():
    out = gazetteer.normalize("🚧 #TrafficAlert @HYDTP Road closed at Paradise Jn. https://t.co/abc", 1)
    assert out == "trafficalert road closed at paradise junction."


def test_expands_abbreviations_on_word_boundaries_only():
    assert gazetteer.normalize("Kachiguda X Roads twds Nampally Rd", 1) == "kachiguda cross roads towards nampally road"
    assert gazetteer.normalize("Board the third car", 1) == "board the third car"          # 'rd' inside words untouched


def test_city_specific_expansions_apply_only_to_that_city():
    assert gazetteer.normalize("PVNR closed", 1) == "pv narasimha rao expressway closed"
    assert gazetteer.normalize("PVNR closed", 2) == "pvnr closed"


def test_collapses_whitespace_and_keeps_slashes_and_digits():
    assert gazetteer.normalize("  Road   No.36 /  14th  Main ", 1) == "road no.36 / 14th main"
