"""
pytest configuration and shared fixtures for cartelera-valencia tests.
"""

import pytest
import sys
from pathlib import Path

# Ensure scraper/ is on the path for all tests
sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Fixture: minimal valid showtime ──────────────────────────────────────────

@pytest.fixture
def showtime_factory():
    """Returns a factory for creating valid showtime dicts."""
    def _make(cinema="babel", language="VO", date="2026-04-21", time="20:00", url="#"):
        return {
            "cinema":   cinema,
            "language": language,
            "date":     date,
            "time":     time,
            "url":      url,
        }
    return _make


@pytest.fixture
def movie_factory(showtime_factory):
    """Returns a factory for creating valid movie dicts."""
    def _make(title="Test Movie", rating=7.5, showtimes=None):
        return {
            "id":                  "tmdb-999",
            "title":               title,
            "title_local":         title,
            "poster":              None,
            "rating":              rating,
            "duration":            110,
            "genres":              ["Drama"],
            "trailer_youtube_id":  None,
            "showtimes":           showtimes or [showtime_factory()],
        }
    return _make


@pytest.fixture
def sample_data(movie_factory, showtime_factory):
    """A minimal but valid showtimes.json data structure."""
    return {
        "updated_at": "2026-04-21T07:00:00Z",
        "movies": [
            movie_factory("Conclave", rating=7.4, showtimes=[
                showtime_factory("babel", "VO",  "2026-04-21", "19:30"),
                showtime_factory("lys",   "VO",  "2026-04-21", "22:00"),
                showtime_factory("yelmo", "ES",  "2026-04-21", "18:00"),
            ]),
            movie_factory("Nosferatu", rating=7.2, showtimes=[
                showtime_factory("babel",     "VO",  "2026-04-21", "22:30"),
                showtime_factory("kinepolis", "ES",  "2026-04-21", "20:00"),
            ]),
        ]
    }
