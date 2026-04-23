"""
Integration-style tests for the orchestrator logic in run.py.
Uses fixtures from conftest.py — no network calls, no Playwright.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from run import build_movie_index, merge_showtimes, normalize_lang, slugify


class TestMovieDeduplication:
    """Tests for the title-based movie deduplication logic."""

    def test_same_title_different_cinemas_merged(self, sample_data, showtime_factory):
        """A title already in the index should have showtimes merged, not duplicated."""
        idx = build_movie_index(sample_data)
        existing = idx["conclave"]

        new_showtimes = [
            showtime_factory("abc", "VO", "2026-04-22", "19:00"),
        ]
        merged = merge_showtimes(existing["showtimes"], new_showtimes)

        titles = [(s["cinema"], s["date"]) for s in merged]
        # Original showtimes still present
        assert ("babel", "2026-04-21") in titles
        # New showtime added
        assert ("abc", "2026-04-22") in titles

    def test_index_is_lowercase(self, sample_data):
        idx = build_movie_index(sample_data)
        assert "conclave"  in idx
        assert "Conclave"  not in idx
        assert "CONCLAVE"  not in idx

    def test_new_movie_not_in_index(self, sample_data):
        idx = build_movie_index(sample_data)
        assert "mickey 17" not in idx

    def test_movie_count(self, sample_data):
        idx = build_movie_index(sample_data)
        assert len(idx) == 2


class TestShowtimeFiltering:
    """Simulate the frontend filter logic in Python for correctness checks."""

    def _filter(self, showtimes, langs=None, cinemas=None, date=None):
        langs   = set(langs   or ["VO", "ES", "VAL"])
        cinemas = set(cinemas or [
            "babel", "lys",
            "abc_park", "abc_elsaler", "abc_granturia",
            "ocine", "dor",
            "yelmo", "kinepolis",
        ])
        return [
            s for s in showtimes
            if s["language"] in langs
            and s["cinema"]  in cinemas
            and (date is None or s["date"] == date)
        ]

    def test_vo_only_filter(self, sample_data):
        all_st = sample_data["movies"][0]["showtimes"]  # Conclave
        result = self._filter(all_st, langs=["VO"])
        assert all(s["language"] == "VO" for s in result)
        assert len(result) == 2  # babel + lys

    def test_es_only_filter(self, sample_data):
        all_st = sample_data["movies"][0]["showtimes"]
        result = self._filter(all_st, langs=["ES"])
        assert all(s["language"] == "ES" for s in result)
        assert len(result) == 1  # yelmo only

    def test_cinema_filter(self, sample_data):
        all_st = sample_data["movies"][0]["showtimes"]
        result = self._filter(all_st, cinemas=["babel"])
        assert all(s["cinema"] == "babel" for s in result)
        assert len(result) == 1

    def test_date_filter(self, sample_data, showtime_factory):
        showtimes = [
            showtime_factory("babel", "VO", "2026-04-21", "18:00"),
            showtime_factory("babel", "VO", "2026-04-22", "18:00"),
            showtime_factory("babel", "VO", "2026-04-23", "18:00"),
        ]
        result = self._filter(showtimes, date="2026-04-22")
        assert len(result) == 1
        assert result[0]["date"] == "2026-04-22"

    def test_no_matching_results(self, sample_data):
        all_st = sample_data["movies"][0]["showtimes"]
        result = self._filter(all_st, langs=["VAL"])
        assert result == []

    def test_all_filters_combined(self, sample_data):
        all_st = sample_data["movies"][0]["showtimes"]
        result = self._filter(all_st, langs=["VO"], cinemas=["lys"], date="2026-04-21")
        assert len(result) == 1
        assert result[0]["cinema"]   == "lys"
        assert result[0]["language"] == "VO"


class TestSlugifyEdgeCases:
    def test_all_cinema_names(self):
        assert slugify("Kinépolis Valencia") == "kinepolis-valencia"
        assert slugify("Cine Babel")         == "cine-babel"
        assert slugify("ABC park")           == "abc-park"

    def test_real_movie_titles(self):
        assert slugify("A Complete Unknown")  == "a-complete-unknown"
        assert slugify("The Brutalist")       == "the-brutalist"
        assert slugify("Mickey 17")           == "mickey-17"
        assert slugify("Anora")               == "anora"
        assert slugify("Cónclave")            == "conclave"

    def test_no_consecutive_dashes(self):
        result = slugify("Hello --- World")
        assert "--" not in result


class TestNormalizeLangEdgeCases:
    def test_mixed_case_variants(self):
        assert normalize_lang("Vo")   == "VO"
        assert normalize_lang("vO")   == "VO"
        assert normalize_lang("Es")   == "ES"
        assert normalize_lang("Val")  == "VAL"

    def test_whitespace_only(self):
        # Should default to ES (unknown)
        assert normalize_lang("   ") == "ES"

    def test_all_known_values_map_correctly(self):
        vo_inputs  = ["VO","vo","V.O.","VOSE","vose","VOS","vos","V.O.S.E.","Original","original"]
        es_inputs  = ["Castellano","castellano","Español","español","ESP","Doblada","doblada","ES","es"]
        val_inputs = ["Valencià","valencià","Valenciano","val","VAL","En valencià"]

        for v in vo_inputs:  assert normalize_lang(v) == "VO",  v
        for v in es_inputs:  assert normalize_lang(v) == "ES",  v
        for v in val_inputs: assert normalize_lang(v) == "VAL", v
