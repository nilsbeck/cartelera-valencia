"""
Tests for cartelera-valencia scraper utilities.

Run: pytest tests/ -v
"""

import json
import os
import sys
from pathlib import Path
from datetime import date, timedelta
import pytest

# Coverage/output tests require a fresh scrape to be meaningful.
# Set AFTER_SCRAPE=1 in CI (scrape.yml does this) to enable them.
_after_scrape = pytest.mark.skipif(
    not os.environ.get("AFTER_SCRAPE"),
    reason="Requires a fresh scrape run — set AFTER_SCRAPE=1",
)

# Make scraper modules importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from run import normalize_lang, normalize_title_key, slugify, merge_showtimes, build_movie_index, validate_per_cinema_per_day
from babel import _parse_date as babel_parse_date, _detect_language as babel_detect_lang
from cinestudio_dor import _parse_date_range, _parse_times, _detect_language as dor_detect_lang
from kinepolis import _detect_language as kine_detect_lang, _extract_json_array
from cinema_abc import (
    _detect_language as abc_detect_lang,
    _detect_lang_from_text as abc_detect_lang_text,
    _parse_ficha_date,
)
from lys import _parse_sesiones_date as lys_parse_date
from ocine import _dates_until_next_thursday, _detect_lang as ocine_detect_lang


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

    def test_verbose_labels(self):
        # Yelmo emits long version labels — VOSE/VO must be detected as substrings
        assert normalize_lang("2D INGLÉS SUBTITULADO EN ESPAÑOL (VOSE)") == "VOSE"
        assert normalize_lang("2D V.O.S.E. - Inglés")                    == "VOSE"
        assert normalize_lang("3D Versión Original")                      == "VO"
        assert normalize_lang("2D Doblado al castellano")                 == "ES"


# ─────────────────────────────────────────────
# normalize_title_key
# ─────────────────────────────────────────────

class TestNormalizeTitleKey:
    def test_year_annotation_stripped(self):
        assert normalize_title_key("TOP GUN (1986) - 40 ANIVERSARIO") == \
               normalize_title_key("Top Gun 40 Aniversario")

    def test_separator_variants(self):
        assert normalize_title_key("Avengers: Endgame") == "avengers endgame"
        assert normalize_title_key("Spider-Man: No Way Home") == "spider man no way home"

    def test_distinct_films_stay_separate(self):
        assert normalize_title_key("Top Gun") != normalize_title_key("Top Gun 40 Aniversario")
        assert normalize_title_key("Top Gun") != normalize_title_key("Top Gun: Maverick")

    def test_case_insensitive(self):
        assert normalize_title_key("THE BATMAN") == normalize_title_key("The Batman")

    def test_extra_whitespace(self):
        assert normalize_title_key("  Dune   Part Two  ") == "dune part two"


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
                    "babel", "lys", "mn4",
                    "abc_park", "abc_elsaler", "abc_granturia",
                    "dor", "yelmo", "kinepolis",
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
# ocine._dates_until_next_thursday
# ─────────────────────────────────────────────

class TestOcineDatesUntilNextThursday:
    def test_always_starts_today(self):
        dates = _dates_until_next_thursday()
        assert dates[0] == date.today().isoformat()

    def test_last_date_is_thursday(self):
        dates = _dates_until_next_thursday()
        last = date.fromisoformat(dates[-1])
        assert last.weekday() == 3  # Thursday

    def test_dates_are_consecutive(self):
        dates = _dates_until_next_thursday()
        for i in range(1, len(dates)):
            d_prev = date.fromisoformat(dates[i - 1])
            d_curr = date.fromisoformat(dates[i])
            assert (d_curr - d_prev).days == 1

    def test_range_is_1_to_7_days(self):
        dates = _dates_until_next_thursday()
        # Minimum: today is Wednesday → [Wed, Thu] = 2 dates
        # Maximum: today is Thursday → [Thu, …, Thu] = 8 dates
        assert 2 <= len(dates) <= 8


# ─────────────────────────────────────────────
# ocine._detect_lang
# ─────────────────────────────────────────────

class TestOcineDetectLang:
    def test_vose_keyword(self):
        assert ocine_detect_lang("VOSE") == "vose"

    def test_vose_dotted(self):
        assert ocine_detect_lang("V.O.S.E.") == "vose"

    def test_vose_via_subtitulada(self):
        assert ocine_detect_lang("versión original subtitulada") == "vose"

    def test_vo_dotted(self):
        assert ocine_detect_lang("V.O.") == "vo"

    def test_vo_via_original(self):
        assert ocine_detect_lang("Versión Original") == "vo"

    def test_vose_beats_original(self):
        # "versión original subtitulada" contains both "subtitulad" and "original"
        # VOSE token must win
        assert ocine_detect_lang("versión original subtitulada en castellano") == "vose"

    def test_es_dubbed(self):
        assert ocine_detect_lang("Doblada") == "es"
        assert ocine_detect_lang("doblado")  == "es"

    def test_es_castellano(self):
        assert ocine_detect_lang("Castellano") == "es"

    def test_unknown_defaults_es(self):
        assert ocine_detect_lang("") == "es"
        assert ocine_detect_lang("4K DolbyAtmos") == "es"

    def test_pipeline_vose_to_canonical(self):
        assert normalize_lang(ocine_detect_lang("VOSE")) == "VOSE"

    def test_pipeline_vo_to_canonical(self):
        assert normalize_lang(ocine_detect_lang("V.O.")) == "VO"

    def test_pipeline_es_to_canonical(self):
        assert normalize_lang(ocine_detect_lang("Doblada")) == "ES"


# ─────────────────────────────────────────────
# cinema_abc._parse_ficha_date
# ─────────────────────────────────────────────

class TestAbcParseFichaDate:
    def test_full_spanish_date(self):
        d = _parse_ficha_date("Jueves, 14 de mayo")
        assert d is not None
        parsed = date.fromisoformat(d)
        assert parsed.month == 5
        assert parsed.day   == 14

    def test_without_de(self):
        d = _parse_ficha_date("Viernes 15 mayo")
        assert d is not None
        assert date.fromisoformat(d).day == 15

    def test_all_months(self):
        months = {
            "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
            "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
            "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
        }
        for name, num in months.items():
            result = _parse_ficha_date(f"Lunes 10 de {name}")
            assert result is not None, f"Failed for {name}"
            assert date.fromisoformat(result).month == num

    def test_invalid_returns_none(self):
        assert _parse_ficha_date("")         is None
        assert _parse_ficha_date("Hoy")      is None
        assert _parse_ficha_date("14 / 05")  is None  # numeric month, not name

    def test_returns_iso_format(self):
        result = _parse_ficha_date("Sábado 17 de mayo")
        assert result is not None
        assert len(result) == 10
        assert result[4] == "-" and result[7] == "-"

    def test_uppercase_month(self):
        d = _parse_ficha_date("JUEVES 14 DE MAYO")
        assert d is not None
        assert date.fromisoformat(d).month == 5

    def test_mixed_case(self):
        d = _parse_ficha_date("Miércoles, 20 de Agosto")
        assert d is not None
        assert date.fromisoformat(d).month == 8
        assert date.fromisoformat(d).day   == 20

    def test_single_digit_day(self):
        d = _parse_ficha_date("Lunes, 5 de junio")
        assert d is not None
        assert date.fromisoformat(d).day == 5


# ─────────────────────────────────────────────
# cinema_abc._detect_lang_from_text
# ─────────────────────────────────────────────

class TestAbcDetectLangFromText:
    """Tests for _detect_lang_from_text — the pure-string helper used by
    both _detect_language (DOM path) and _scrape_ficha (JS path)."""

    def test_vose_exact(self):
        assert abc_detect_lang_text("VOSE") == "vose"
        assert abc_detect_lang_text("vose") == "vose"

    def test_vose_in_parentheses(self):
        assert abc_detect_lang_text("(VOSE)") == "vose"

    def test_vose_dotted(self):
        # The page labels sessions as "(VOSE)", not "V.O.S.E.".
        # "v.o.s.e." (dotted, exact) falls under the VO exact-match list.
        assert abc_detect_lang_text("v.o.s.e.") == "vo"
        # "V.O.S.E. subtítulos" has no "vose" substring and is not an exact
        # match, so it returns "es" — the site never emits this string.
        assert abc_detect_lang_text("V.O.S.E. subtítulos") == "es"

    def test_vo_exact_forms(self):
        for label in ("vo", "v.o.", "v.o.s.", "v.o.s.e."):
            assert abc_detect_lang_text(label) == "vo", f"Expected vo for {label!r}"

    def test_vo_case_insensitive(self):
        assert abc_detect_lang_text("V.O.") == "vo"
        assert abc_detect_lang_text("VO")   == "vo"

    def test_empty_is_es(self):
        assert abc_detect_lang_text("") == "es"

    def test_whitespace_is_es(self):
        assert abc_detect_lang_text("   ") == "es"

    def test_technical_badge_is_es(self):
        assert abc_detect_lang_text("4K")      == "es"
        assert abc_detect_lang_text("PREMIUM") == "es"
        assert abc_detect_lang_text("ATMOS")   == "es"

    def test_pipeline_vose(self):
        assert normalize_lang(abc_detect_lang_text("(VOSE)")) == "VOSE"

    def test_pipeline_vo(self):
        assert normalize_lang(abc_detect_lang_text("V.O.")) == "VO"

    def test_pipeline_es(self):
        assert normalize_lang(abc_detect_lang_text("")) == "ES"


# ─────────────────────────────────────────────
# cinema_abc._detect_language  (DOM element path)
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
        assert abc_detect_lang(self._FakeEl("4K")) == "es"

    def test_vo_label(self):
        assert abc_detect_lang(self._FakeEl("V.O.")) == "vo"

    def test_delegates_to_detect_lang_from_text(self):
        # DOM element path and string path must agree
        for label in ("(VOSE)", "V.O.", "", "4K"):
            assert abc_detect_lang(self._FakeEl(label)) == abc_detect_lang_text(label)



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
    def test_per_cinema_per_day_coverage(self):
        """Every cinema must have at least 1 movie for each day in its expected window."""
        from datetime import timedelta

        with open(self.DATA_FILE) as f:
            data = json.load(f)

        today = date.today()
        std_dates = {(today + timedelta(days=i)).isoformat() for i in range(7)}
        # Lys/MN4 may legitimately drop today after past sessions are over —
        # see validate_per_cinema_per_day in run.py.
        future_only = std_dates - {today.isoformat()}

        expected = {
            "babel":         std_dates,
            "lys":           future_only,
            "mn4":           future_only,
            "abc_park":      std_dates,
            "abc_elsaler":   std_dates,
            "abc_granturia": std_dates,
            "dor":           std_dates,
            "yelmo":         std_dates,
            "kinepolis":     std_dates,
        }

        covered = set()
        for movie in data.get("movies", []):
            for st in movie.get("showtimes", []):
                covered.add((st["cinema"], st["date"]))

        missing = []
        for cinema, dates in sorted(expected.items()):
            for d in sorted(dates):
                if (cinema, d) not in covered:
                    missing.append(f"{cinema} on {d}")

        assert not missing, (
            "Every cinema must have at least 1 movie for each expected day.\n"
            "Missing:\n" + "\n".join(f"  {m}" for m in missing)
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


# ─────────────────────────────────────────────
# Yelmo language pipeline
# ─────────────────────────────────────────────

class TestYelmoLanguagePipeline:
    """Yelmo emits verbose version labels.  Encode the expected mapping here
    so that any change to normalize_lang that breaks Yelmo is caught in CI."""

    def test_vose_verbose_2d(self):
        assert normalize_lang("2D INGLÉS SUBTITULADO EN ESPAÑOL (VOSE)") == "VOSE"

    def test_vose_verbose_3d(self):
        assert normalize_lang("3D INGLÉS SUBTITULADO EN ESPAÑOL (VOSE)") == "VOSE"

    def test_es_2d_espanol(self):
        assert normalize_lang("2D ESPAÑOL") == "ES"

    def test_es_3d_castellano(self):
        assert normalize_lang("3D CASTELLANO") == "ES"

    def test_empty_defaults_es(self):
        assert normalize_lang("") == "ES"

    def test_yelmo_pipeline_in_health_check(self):
        """Matches the TestCrawlerHealthNormalization contract for Yelmo."""
        assert normalize_lang("2D INGLÉS SUBTITULADO EN ESPAÑOL (VOSE)") == "VOSE"
        assert normalize_lang("2D ESPAÑOL") == "ES"
        assert normalize_lang("3D CASTELLANO") == "ES"


# ─────────────────────────────────────────────
# validate_per_cinema_per_day
# ─────────────────────────────────────────────

class TestValidatePerCinemaPerDay:
    ALL_CINEMAS = [
        "babel", "lys", "mn4",
        "abc_park", "abc_elsaler", "abc_granturia",
        "dor", "yelmo", "kinepolis",
    ]

    def _full_coverage(self):
        today = date.today()
        return [
            {"cinema": cinema, "date": (today + timedelta(days=i)).isoformat()}
            for cinema in self.ALL_CINEMAS
            for i in range(7)
        ]

    def test_full_coverage_returns_empty(self):
        assert validate_per_cinema_per_day(self._full_coverage()) == []

    def test_missing_cinema_returns_seven_warnings(self):
        rows = [r for r in self._full_coverage() if r["cinema"] != "dor"]
        warnings = validate_per_cinema_per_day(rows)
        assert len(warnings) == 7
        assert all("dor" in w for w in warnings)

    def test_missing_one_day_returns_one_warning(self):
        today = date.today().isoformat()
        rows = [r for r in self._full_coverage()
                if not (r["cinema"] == "yelmo" and r["date"] == today)]
        warnings = validate_per_cinema_per_day(rows)
        assert len(warnings) == 1
        assert "yelmo" in warnings[0]
        assert today in warnings[0]

    def test_never_raises(self):
        result = validate_per_cinema_per_day([])
        assert isinstance(result, list)
        assert len(result) > 0  # empty input means everything is missing

    def test_returns_list_not_exception(self):
        # Regression: old behaviour was to raise RuntimeError
        try:
            result = validate_per_cinema_per_day([])
            assert isinstance(result, list)
        except RuntimeError:
            pytest.fail("validate_per_cinema_per_day must return warnings, not raise")


# ─────────────────────────────────────────────
# build_movie_index with normalised titles
# ─────────────────────────────────────────────

class TestBuildMovieIndexNormalized:
    def _movie(self, title, *, has_tmdb=False):
        return {
            "title": title,
            "poster": "p.jpg" if has_tmdb else None,
            "rating": 7.5 if has_tmdb else None,
            "trailer_youtube_id": "x" if has_tmdb else None,
            "showtimes": [],
        }

    def test_year_annotation_found_by_clean_key(self):
        existing = {"movies": [self._movie("Top Gun (1986) - 40 Aniversario", has_tmdb=True)]}
        index = build_movie_index(existing)
        # Scraped title without year should resolve to same index entry
        assert normalize_title_key("Top Gun 40 Aniversario") in index

    def test_tmdb_enriched_entry_wins_over_bare(self):
        existing = {"movies": [
            self._movie("Top Gun (1986)", has_tmdb=True),
            self._movie("Top Gun",        has_tmdb=False),
        ]}
        index = build_movie_index(existing)
        key = normalize_title_key("Top Gun")
        assert index[key]["poster"] == "p.jpg"

    def test_showtimes_merged_across_title_variants(self):
        st1 = {"cinema": "yelmo",     "language": "ES", "date": "2026-05-14", "time": "18:00"}
        st2 = {"cinema": "kinepolis", "language": "ES", "date": "2026-05-14", "time": "20:00"}
        existing = {"movies": [
            {**self._movie("Top Gun (1986)"), "showtimes": [st1]},
            {**self._movie("Top Gun"),        "showtimes": [st2]},
        ]}
        index = build_movie_index(existing)
        key = normalize_title_key("Top Gun")
        assert len(index[key]["showtimes"]) == 2


# ─────────────────────────────────────────────
# Cinestudio d'Or retry logic
# ─────────────────────────────────────────────

class TestDorRetryLogic:
    """Verify cinestudio_dor.scrape()'s two-source flow:
       1 try on the Atom feed, then 4-attempt exponential backoff on the
       homepage if the feed yielded no entries.
    """

    def _mock_ok(self):
        from unittest.mock import Mock
        r = Mock()
        # Empty body — no <entry> for the feed path, no div.post-outer for
        # the homepage path; both code paths return an empty result list.
        r.text = "<html><body></body></html>"
        r.raise_for_status = Mock()
        return r

    def _mock_fail(self):
        from unittest.mock import Mock
        r = Mock()
        r.raise_for_status = Mock(side_effect=Exception("429 Too Many Requests"))
        return r

    def test_success_on_first_attempt(self):
        """Feed succeeds but has no entries → falls back to homepage, which
        succeeds on attempt 1. Total: 1 feed call + 1 homepage call = 2."""
        from unittest.mock import patch
        import cinestudio_dor
        with patch("cinestudio_dor.requests.get", return_value=self._mock_ok()) as m:
            cinestudio_dor.scrape()
            assert m.call_count == 2

    def test_retries_after_failure(self):
        """Feed fails, homepage retries: attempt 1 fails, attempt 2 succeeds.
        Total: 1 + 2 = 3 calls."""
        from unittest.mock import patch
        import cinestudio_dor
        side_effects = [self._mock_fail(), self._mock_fail(), self._mock_ok()]
        with patch("cinestudio_dor.requests.get", side_effect=side_effects) as m:
            with patch("cinestudio_dor.time.sleep"):
                cinestudio_dor.scrape()
            assert m.call_count == 3

    def test_all_retries_exhausted_returns_empty(self):
        """Every call fails: 1 feed attempt + 4 homepage attempts = 5."""
        from unittest.mock import patch
        import cinestudio_dor
        with patch("cinestudio_dor.requests.get", return_value=self._mock_fail()) as m:
            with patch("cinestudio_dor.time.sleep"):
                result = cinestudio_dor.scrape()
            assert result == []
            assert m.call_count == 5  # 1 feed + 4 homepage attempts (delays 0, 2, 4, 8)
