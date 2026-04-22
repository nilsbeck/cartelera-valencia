"""
Tests for cartelera-valencia scraper utilities.

Run: pytest tests/ -v
"""

import json
import sys
from pathlib import Path
from datetime import date

# Make scraper modules importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from run import normalize_lang, slugify, merge_showtimes, build_movie_index


# ─────────────────────────────────────────────
# normalize_lang
# ─────────────────────────────────────────────

class TestNormalizeLang:
    def test_vo_variants(self):
        for raw in ["VO", "vo", "V.O.", "v.o.", "VOSE", "vose", "VOS", "vos",
                    "V.O.S.E.", "v.o.s.e.", "Original", "original"]:
            assert normalize_lang(raw) == "VO", f"Expected VO for {raw!r}"

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
        assert normalize_lang("  VO  ")  == "VO"
        assert normalize_lang("\tVOSE\n") == "VO"


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
                assert s["cinema"]   in {"babel","lys","abc","yelmo","kinepolis"}, \
                    f"Unknown cinema: {s['cinema']}"
                assert s["language"] in {"VO","ES","VAL"}, \
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
