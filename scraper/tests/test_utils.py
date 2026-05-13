"""
Tests for cartelera-valencia scraper utilities.

Run: pytest tests/ -v
"""

import json
import os
import sys
from pathlib import Path
from datetime import date
import pytest

# Coverage/output tests require a fresh scrape to be meaningful.
# Set AFTER_SCRAPE=1 in CI (scrape.yml does this) to enable them.
_after_scrape = pytest.mark.skipif(
    not os.environ.get("AFTER_SCRAPE"),
    reason="Requires a fresh scrape run — set AFTER_SCRAPE=1",
)

# Make scraper modules importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from run import normalize_lang, slugify, merge_showtimes, build_movie_index
from babel import _parse_date as babel_parse_date, _detect_language as babel_detect_lang
from cinestudio_dor import _parse_date_range, _parse_times, _detect_language as dor_detect_lang
from kinepolis import _detect_language as kine_detect_lang, _extract_json_array
from cinema_abc import _detect_language as abc_detect_lang
from lys import _parse_sesiones_date as lys_parse_date


# ─────────────────────────────────────────────
# normalize_lang
# ─────────────────────────────────────────────

class TestNormalizeLang:
    def test_vo_variants(self):
        for raw in ["VO", "vo", "V.O.", "v.o.", "Original", "original"]:
            assert normalize_lang(raw) == "VO", f"Expected VO for {raw!r}"

    def test_vose_variants(self):
        for raw in ["VOSE", "vose", "VOS", "vos", "V.O.S.E.", "v.o.s.e.", "V.O.S.", "v.o.s."]:
            assert normalize_lang(raw) == "VOSE", f"Expected VOSE for {raw!r}"

    def test_es_variants(self):
        for raw in ["Castellano", "castellano", "Español", "español",
                    "ESP", "esp", "Doblada", "doblada", "Doblado", "ES", "es"]:
            assert normalize_lang(raw) == "ES", f"Expected ES for {raw!r}"

    def test_val_variants(self):
        for raw in ["Valencià", "valencià", "Valenciano", "valenciano",
                    "VAL", "val", "En valencià", "en valencià"]:
            assert normalize_lang(raw) == "VAL", f"Expected VAL for {raw!r}"

    def test_unknown_falls_back_to_es(self):
        assert normalize_lang("???")        == "ES"
        assert normalize_lang("")           == "ES"
        assert normalize_lang("subtitulada")== "ES"

    def test_strips_whitespace(self):
        assert normalize_lang("  VO  ")   == "VO"
        assert normalize_lang("\tVOSE\n") == "VOSE"


# ─────────────────────────────────────────────
# slugify
# ─────────────────────────────────────────────

class TestSlugify:
    def test_basic(self):
        assert slugify("Conclave") == "conclave"

    def test_spaces_become_dashes(self):
        assert slugify("A Complete Unknown") == "a-complete-unknown"

    def test_accents_stripped(self):
        assert slugify("Nosferatu el Vampiro") == "nosferatu-el-vampiro"
        assert slugify("Cónclave")             == "conclave"
        assert slugify("L'Été dernier")        == "l-ete-dernier"

    def test_special_chars_removed(self):
        assert slugify("Mission: Impossible") == "mission-impossible"
        assert slugify("Spider-Man!")         == "spider-man"

    def test_no_leading_trailing_dashes(self):
        result = slugify("  !Title!  ")
        assert not result.startswith("-")
        assert not result.endswith("-")

    def test_numbers_kept(self):
        assert slugify("Mickey 17") == "mickey-17"


# ─────────────────────────────────────────────
# merge_showtimes
# ─────────────────────────────────────────────

class TestMergeShowtimes:
    BASE = [
        {"cinema": "babel", "language": "VO",  "date": "2026-04-21", "time": "18:00", "url": "#"},
        {"cinema": "babel", "language": "VO",  "date": "2026-04-21", "time": "21:00", "url": "#"},
    ]

    def test_no_duplicates_added(self):
        result = merge_showtimes(self.BASE, self.BASE)
        assert len(result) == 2

    def test_new_showtimes_appended(self):
        new = [{"cinema": "lys", "language": "VO", "date": "2026-04-21", "time": "20:00", "url": "#"}]
        result = merge_showtimes(self.BASE, new)
        assert len(result) == 3
        assert any(s["cinema"] == "lys" for s in result)

    def test_partial_overlap(self):
        new = [
            {"cinema": "babel", "language": "VO",  "date": "2026-04-21", "time": "18:00", "url": "#"},  # dup
            {"cinema": "babel", "language": "ES",  "date": "2026-04-21", "time": "19:00", "url": "#"},  # new
        ]
        result = merge_showtimes(self.BASE, new)
        assert len(result) == 3

    def test_empty_existing(self):
        new = [{"cinema": "yelmo", "language": "ES", "date": "2026-04-21", "time": "17:00", "url": "#"}]
        result = merge_showtimes([], new)
        assert len(result) == 1

    def test_empty_new(self):
        result = merge_showtimes(self.BASE, [])
        assert len(result) == 2

    def test_different_date_not_duplicate(self):
        new = [{"cinema": "babel", "language": "VO", "date": "2026-04-22", "time": "18:00", "url": "#"}]
        result = merge_showtimes(self.BASE, new)
        assert len(result) == 3


# ─────────────────────────────────────────────
# build_movie_index
# ─────────────────────────────────────────────

class TestBuildMovieIndex:
    def test_basic_index(self):
        data = {"movies": [
            {"title": "Conclave",          "showtimes": []},
            {"title": "A Complete Unknown","showtimes": []},
        ]}
        idx = build_movie_index(data)
        assert "conclave"           in idx
        assert "a complete unknown" in idx

    def test_case_insensitive(self):
        data = {"movies": [{"title": "NOSFERATU", "showtimes": []}]}
        idx = build_movie_index(data)
        assert "nosferatu" in idx

    def test_empty_data(self):
        assert build_movie_index({}) == {}
        assert build_movie_index({"movies": []}) == {}

    def test_returns_full_movie_object(self):
        movie = {"title": "Anora", "rating": 7.8, "showtimes": []}
        idx = build_movie_index({"movies": [movie]})
        assert idx["anora"]["rating"] == 7.8


# ─────────────────────────────────────────────
# JSON schema validation
# ─────────────────────────────────────────────

class TestShowtimesSchema:
    """Validates that a showtimes.json file (if present) has correct structure."""

    DATA_FILE = Path(__file__).parent.parent.parent / "data" / "showtimes.json"

    def test_schema_if_file_exists(self):
        if not self.DATA_FILE.exists():
            return  # skip in CI before first scrape run

        with open(self.DATA_FILE) as f:
            data = json.load(f)

        assert "updated_at" in data
        assert "movies"     in data
        assert isinstance(data["movies"], list)

        for movie in data["movies"]:
            assert "title"     in movie, f"Missing title: {movie}"
            assert "showtimes" in movie, f"Missing showtimes: {movie}"
            assert isinstance(movie["showtimes"], list)

            for s in movie["showtimes"]:
                assert s["cinema"] in {
                    "babel", "lys",
                    "abc_park", "abc_elsaler", "abc_granturia",
                    "ocine", "dor",
                    "yelmo", "kinepolis",
                }, f"Unknown cinema: {s['cinema']}"
                assert s["language"] in {"VO", "VOSE", "ES", "VAL"}, \
                    f"Unknown language: {s['language']}"
                assert len(s["date"]) == 10, f"Bad date format: {s['date']}"
                assert ":" in s["time"],     f"Bad time format: {s['time']}"

    def test_updated_at_is_today_or_recent(self):
        if not self.DATA_FILE.exists():
            return

        with open(self.DATA_FILE) as f:
            data = json.load(f)

        updated = data["updated_at"][:10]   # YYYY-MM-DD
        updated_date = date.fromisoformat(updated)
        delta = (date.today() - updated_date).days
        assert delta <= 2, f"showtimes.json is stale ({delta} days old)"


# ─────────────────────────────────────────────
# babel._parse_date
# ─────────────────────────────────────────────

class TestBabelParseDate:
    def test_basic(self):
        d = babel_parse_date("Mié 23 Abr")
        assert d is not None
        parsed = date.fromisoformat(d)
        assert parsed.month == 4
        assert parsed.day   == 23

    def test_all_months(self):
        months = {
            "Ene": 1, "Feb": 2, "Mar": 3, "Abr": 4, "May": 5, "Jun": 6,
            "Jul": 7, "Ago": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dic": 12,
        }
        for abbr, num in months.items():
            result = babel_parse_date(f"Lun 15 {abbr}")
            assert result is not None, f"Failed for {abbr}"
            assert date.fromisoformat(result).month == num

    def test_invalid_returns_none(self):
        assert babel_parse_date("")          is None
        assert babel_parse_date("Hoy")       is None
        assert babel_parse_date("Mié Abr")   is None  # missing day number
        assert babel_parse_date("22")        is None  # too short

    def test_returns_iso_format(self):
        result = babel_parse_date("Jue 24 Abr")
        assert result is not None
        assert len(result) == 10
        assert result[4] == "-"
        assert result[7] == "-"


# ─────────────────────────────────────────────
# babel._detect_language
# ─────────────────────────────────────────────

class TestBabelDetectLanguage:
    """babel._detect_language uses a mock block object; we call it via a
    helper that creates a playwright-like mock with query_selector_all."""

    def _make_block(self, divs: list[str]):
        """Minimal mock that makes _detect_language work without Playwright."""
        class FakeEl:
            def __init__(self, text): self._text = text
            def inner_text(self): return self._text

        class FakeBlock:
            def __init__(self, texts): self._texts = texts
            def query_selector_all(self, sel):
                return [FakeEl(t) for t in self._texts]

        return FakeBlock(divs)

    def test_spanish_audio_no_subtitles(self):
        block = self._make_block(["Idioma: Español", ""])
        assert babel_detect_lang(block) == "es"

    def test_english_with_subtitles(self):
        block = self._make_block(["Idioma: Inglés", "Subtítulos: Castellano"])
        assert babel_detect_lang(block) == "vose"

    def test_english_no_subtitles(self):
        block = self._make_block(["Idioma: Inglés"])
        assert babel_detect_lang(block) == "vo"

    def test_castellano_is_es(self):
        block = self._make_block(["Idioma: Castellano"])
        assert babel_detect_lang(block) == "es"

    def test_empty_block_defaults_es(self):
        block = self._make_block([])
        assert babel_detect_lang(block) == "es"


# ─────────────────────────────────────────────
# cinestudio_dor._parse_date_range
# ─────────────────────────────────────────────

class TestDorParseDateRange:
    def test_same_month(self):
        dates = _parse_date_range("20 — 26 abril")
        assert len(dates) == 7
        assert dates[0].month == 4 and dates[0].day == 20
        assert dates[-1].month == 4 and dates[-1].day == 26

    def test_cross_month(self):
        dates = _parse_date_range("27 abril — 3 mayo")
        assert len(dates) == 7
        assert dates[0].month  == 4
        assert dates[0].day    == 27
        assert dates[-1].month == 5
        assert dates[-1].day   == 3

    def test_single_day(self):
        dates = _parse_date_range("15 — 15 mayo")
        assert len(dates) == 1
        assert dates[0].month == 5
        assert dates[0].day   == 15

    def test_invalid_returns_empty(self):
        assert _parse_date_range("")             == []
        assert _parse_date_range("sin fecha")    == []
        assert _parse_date_range("20 abril")     == []   # no separator

    def test_dates_are_consecutive(self):
        dates = _parse_date_range("10 — 14 junio")
        for i in range(1, len(dates)):
            assert (dates[i] - dates[i-1]).days == 1


# ─────────────────────────────────────────────
# cinestudio_dor._parse_times
# ─────────────────────────────────────────────

class TestDorParseTimes:
    def test_two_times(self):
        assert _parse_times("16:30h. 20:30h.") == ["16:30", "20:30"]

    def test_parenthetical_stripped(self):
        # "(L 18:05h.)" means Monday-only — should be ignored
        result = _parse_times("18:05h. 22:05h. (L 18:05h.)")
        assert result == ["18:05", "22:05"]

    def test_single_time(self):
        assert _parse_times("20:00h.") == ["20:00"]

    def test_no_times_returns_empty(self):
        assert _parse_times("versión doblada") == []
        assert _parse_times("")                == []

    def test_no_trailing_h(self):
        # Should also work without the 'h.' suffix
        assert _parse_times("16:30 20:30") == ["16:30", "20:30"]


# ─────────────────────────────────────────────
# cinestudio_dor._detect_language
# ─────────────────────────────────────────────

class TestDorDetectLanguage:
    def test_dubbed(self):
        assert dor_detect_lang("versión doblada / digital") == "es"
        assert dor_detect_lang("DOBLADA")                   == "es"

    def test_original_with_subtitles(self):
        assert dor_detect_lang("versión original / subtítulos en castellano") == "vose"
        assert dor_detect_lang("VOSE")                                        == "vose"
        assert dor_detect_lang("con subtítulos")                              == "vose"

    def test_original_no_subtitles(self):
        assert dor_detect_lang("versión original")    == "vo"
        assert dor_detect_lang("V.O. sin subtítulos") == "vo"

    def test_unknown_defaults_es(self):
        assert dor_detect_lang("digital") == "es"
        assert dor_detect_lang("")        == "es"


# ─────────────────────────────────────────────
# kinepolis._detect_language
# ─────────────────────────────────────────────

class TestKinepolisDetectLanguage:
    def test_spanish(self):
        assert kine_detect_lang("2D,nosubt,Spanish", []) == "es"

    def test_english_no_subtitles(self):
        assert kine_detect_lang("2D,nosubt,English", []) == "vo"

    def test_vose_via_raw(self):
        assert kine_detect_lang("2D,VOSE,English", []) == "vose"

    def test_vose_via_session_attr(self):
        attrs = [{"code": "VOSE"}]
        assert kine_detect_lang("2D,English", attrs) == "vose"

    def test_spanish_subtitles_in_raw(self):
        assert kine_detect_lang("2D,Span Subt,English", []) == "vose"

    def test_case_insensitive(self):
        assert kine_detect_lang("2d,vose,english", []) == "vose"
        assert kine_detect_lang("2D,SPANISH", [])      == "es"


# ─────────────────────────────────────────────
# kinepolis._extract_json_array
# ─────────────────────────────────────────────

class TestKinepolisExtractJsonArray:
    def test_simple_array(self):
        html = 'prefix "items":[{"a":1},{"a":2}] suffix'
        result = _extract_json_array(html, '"items":[')
        assert result == [{"a": 1}, {"a": 2}]

    def test_nested_array(self):
        html = '"data":[{"x":[1,2]},{"x":[3]}]'
        result = _extract_json_array(html, '"data":[')
        assert len(result) == 2
        assert result[0]["x"] == [1, 2]

    def test_marker_not_found_returns_empty(self):
        assert _extract_json_array("no match here", '"missing":[') == []

    def test_string_with_brackets(self):
        # Brackets inside strings should not confuse the parser
        html = '"arr":[{"msg":"[hello]"},{"msg":"world"}]'
        result = _extract_json_array(html, '"arr":[')
        assert len(result) == 2
        assert result[0]["msg"] == "[hello]"

    def test_empty_array(self):
        html = '"sessions":[]'
        result = _extract_json_array(html, '"sessions":[')
        assert result == []


# ─────────────────────────────────────────────
# lys._parse_sesiones_date
# ─────────────────────────────────────────────

class TestLysParseDate:
    def test_standard_format(self):
        d = lys_parse_date("Ju 23 / 04")
        assert d is not None
        parsed = date.fromisoformat(d)
        assert parsed.month == 4
        assert parsed.day   == 23

    def test_compact_format(self):
        d = lys_parse_date("Ju23/04")
        assert d is not None
        assert date.fromisoformat(d).day == 23

    def test_invalid_returns_none(self):
        assert lys_parse_date("")        is None
        assert lys_parse_date("Hoy")     is None
        assert lys_parse_date("23 abr")  is None

    def test_returns_iso_format(self):
        result = lys_parse_date("Vi 15 / 05")
        assert result is not None
        assert len(result) == 10
        assert result[4] == "-" and result[7] == "-"


# ─────────────────────────────────────────────
# cinema_abc._detect_language
# ─────────────────────────────────────────────

class TestAbcDetectLanguage:
    class _FakeEl:
        def __init__(self, text): self._text = text
        def inner_text(self): return self._text

    def test_vose_label(self):
        assert abc_detect_lang(self._FakeEl("(VOSE)")) == "vose"

    def test_vose_case_insensitive(self):
        assert abc_detect_lang(self._FakeEl("vose")) == "vose"
        assert abc_detect_lang(self._FakeEl("VOSE")) == "vose"

    def test_empty_label_is_es(self):
        assert abc_detect_lang(self._FakeEl("")) == "es"

    def test_none_element_is_es(self):
        assert abc_detect_lang(None) == "es"

    def test_4k_label_is_es(self):
        # Technical badge like "4K" should not be detected as VO
        assert abc_detect_lang(self._FakeEl("4K")) == "es"

    def test_vo_label(self):
        assert abc_detect_lang(self._FakeEl("V.O.")) == "vo"


# ─────────────────────────────────────────────
# Crawler health: end-to-end normalization pipeline
# ─────────────────────────────────────────────

class TestCrawlerHealthNormalization:
    """Verify that each scraper's raw language output flows correctly through
    normalize_lang. These tests encode the contract between scrapers and the
    normalization layer — if either side breaks, CI fails."""

    def test_abc_vose_pipeline(self):
        """ABC Park (VOSE) → raw 'vose' → canonical 'VOSE'."""
        fake_el = TestAbcDetectLanguage._FakeEl("(VOSE)")
        raw = abc_detect_lang(fake_el)
        assert normalize_lang(raw) == "VOSE"

    def test_abc_es_pipeline(self):
        """ABC Park (dubbed) → raw 'es' → canonical 'ES'."""
        raw = abc_detect_lang(None)
        assert normalize_lang(raw) == "ES"

    def test_babel_vose_pipeline(self):
        """Babel (foreign audio + subtitles) → raw 'vose' → canonical 'VOSE'."""
        class FakeBlock:
            def query_selector_all(self, sel):
                class FakeEl:
                    def __init__(self, t): self._t = t
                    def inner_text(self): return self._t
                return [FakeEl("Idioma: Inglés"), FakeEl("Subtítulos: Castellano")]
        raw = babel_detect_lang(FakeBlock())
        assert normalize_lang(raw) == "VOSE"

    def test_kinepolis_vose_pipeline(self):
        """Kinépolis VOSE → raw 'vose' → canonical 'VOSE'."""
        raw = kine_detect_lang("2D,VOSE,English", [])
        assert normalize_lang(raw) == "VOSE"

    def test_dor_vose_pipeline(self):
        """Cinestudio d'Or subtitles → raw 'vose' → canonical 'VOSE'."""
        raw = dor_detect_lang("versión original / subtítulos en castellano")
        assert normalize_lang(raw) == "VOSE"

    def test_lys_vose_pipeline(self):
        """Lys span.label-cinema 'VOSE' → normalize_lang → canonical 'VOSE'."""
        assert normalize_lang("VOSE") == "VOSE"

    def test_no_raw_language_falls_back_to_es(self):
        """Missing/empty language tag defaults to 'ES', not silent drop."""
        assert normalize_lang("") == "ES"
        assert normalize_lang("unknown_tag_xyz") == "ES"


# ─────────────────────────────────────────────
# Crawler health: showtimes.json cinema coverage
# ─────────────────────────────────────────────

class TestCrawlerCoverage:
    """After a scrape run the major commercial cinemas must have showtimes.
    Smaller or art-house venues (lys, ocine, dor) are excluded because they
    have irregular schedules or less stable scrapers — their absence does not
    reliably indicate a breakage."""

    DATA_FILE = Path(__file__).parent.parent.parent / "data" / "showtimes.json"

    # Large multiplexes that run every day — missing any of these is a red flag.
    CORE_CINEMAS = {"abc_park", "abc_elsaler", "abc_granturia", "yelmo", "kinepolis"}

    def _load_today_showtimes(self):
        with open(self.DATA_FILE) as f:
            data = json.load(f)
        today = date.today().isoformat()
        return [
            s
            for m in data.get("movies", [])
            for s in m.get("showtimes", [])
            if s["date"] >= today
        ]

    @_after_scrape
    def test_core_cinemas_have_future_showtimes(self):
        showtimes = self._load_today_showtimes()
        active_cinemas = {s["cinema"] for s in showtimes}
        missing = self.CORE_CINEMAS - active_cinemas
        assert not missing, (
            f"Core cinemas missing from today's data (scraper likely broken): "
            f"{', '.join(sorted(missing))}"
        )

    @_after_scrape
    def test_minimum_movie_count(self):
        with open(self.DATA_FILE) as f:
            data = json.load(f)
        today = date.today().isoformat()
        active_movies = [
            m for m in data.get("movies", [])
            if any(s["date"] >= today for s in m.get("showtimes", []))
        ]
        assert len(active_movies) >= 5, (
            f"Only {len(active_movies)} movies with upcoming showtimes — "
            "scraper may have failed silently"
        )
