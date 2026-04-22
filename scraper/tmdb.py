"""
TMDB enrichment — looks up a movie title and returns
poster URL, rating, duration, genres, trailer YouTube ID.

Requires env var: TMDB_API_KEY
"""

import os
import requests

TMDB_KEY  = os.environ.get("TMDB_API_KEY", "")
BASE_URL  = "https://api.themoviedb.org/3"
IMG_BASE  = "https://image.tmdb.org/t/p/w342"  # 342px wide poster
HEADERS   = {"accept": "application/json"}
LANG      = "es-ES"   # prefer Spanish metadata


def enrich_movie(title: str) -> dict | None:
    if not TMDB_KEY:
        print("  ⚠ TMDB_API_KEY not set, skipping enrichment")
        return None

    # 1. Search
    try:
        r = requests.get(
            f"{BASE_URL}/search/movie",
            params={"api_key": TMDB_KEY, "query": title, "language": LANG},
            headers=HEADERS,
            timeout=8,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
    except Exception as e:
        print(f"  ✗ TMDB search failed: {e}")
        return None

    if not results:
        print(f"  ✗ No TMDB result for: {title}")
        return None

    movie = results[0]
    tmdb_id = movie["id"]
    print(f"  ✓ Found: {movie.get('title')} (id={tmdb_id})")

    # 2. Details (runtime + genres)
    details = {}
    try:
        r2 = requests.get(
            f"{BASE_URL}/movie/{tmdb_id}",
            params={"api_key": TMDB_KEY, "language": LANG},
            headers=HEADERS,
            timeout=8,
        )
        r2.raise_for_status()
        details = r2.json()
    except Exception as e:
        print(f"  ⚠ TMDB details failed: {e}")

    # 3. Videos (trailer)
    trailer_yt_id = None
    try:
        r3 = requests.get(
            f"{BASE_URL}/movie/{tmdb_id}/videos",
            params={"api_key": TMDB_KEY, "language": "en-US"},
            headers=HEADERS,
            timeout=8,
        )
        r3.raise_for_status()
        videos = r3.json().get("results", [])
        # Prefer official YouTube trailers
        for v in videos:
            if v.get("site") == "YouTube" and v.get("type") == "Trailer":
                trailer_yt_id = v["key"]
                break
        if not trailer_yt_id:
            for v in videos:
                if v.get("site") == "YouTube":
                    trailer_yt_id = v["key"]
                    break
    except Exception as e:
        print(f"  ⚠ TMDB videos failed: {e}")

    poster_path = movie.get("poster_path") or details.get("poster_path")

    return {
        "id":                 f"tmdb-{tmdb_id}",
        "title_local":        details.get("title") or movie.get("title", ""),
        "poster_url":         f"{IMG_BASE}{poster_path}" if poster_path else None,
        "rating":             round(movie.get("vote_average", 0), 1) or None,
        "duration":           details.get("runtime") or None,
        "genres":             [g["name"] for g in details.get("genres", [])[:3]],
        "trailer_youtube_id": trailer_yt_id,
    }
