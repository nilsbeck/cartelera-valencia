#!/usr/bin/env python3
"""
Cartelera Valencia — main scraper orchestrator
Run: python scraper/run.py
Writes: data/showtimes.json, posters/<slug>.jpg
"""

import json
import os
import re
import sys
import time
import hashlib
import requests
from datetime import date, timedelta
from pathlib import Path

# ── Cinema scrapers (one per file)
from babel     import scrape as scrape_babel
from lys       import scrape as scrape_lys
from cinema_abc import scrape as scrape_abc
from yelmo     import scrape as scrape_yelmo
from kinepolis import scrape as scrape_kinepolis
from tmdb              import enrich_movie

# ── Paths
ROOT        = Path(__file__).parent.parent
DATA_FILE   = ROOT / "data" / "showtimes.json"
POSTERS_DIR = ROOT / "posters"
POSTERS_DIR.mkdir(parents=True, exist_ok=True)
(ROOT / "data").mkdir(parents=True, exist_ok=True)

# ── Language normalization
LANG_MAP = {
    # → VO
    "vo": "VO", "v.o.": "VO", "original": "VO", "vose": "VO",
    "v.o.s.e.": "VO", "vos": "VO", "v.o.s.": "VO",
    # → ES
    "castellano": "ES", "español": "ES", "esp": "ES",
    "doblada": "ES", "doblado": "ES", "es": "ES",
    # → VAL
    "valencià": "VAL", "valenciano": "VAL", "val": "VAL",
    "en valencià": "VAL",
}

def normalize_lang(raw: str) -> str:
    return LANG_MAP.get(raw.strip().lower(), "ES")


def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[àáâã]", "a", text)
    text = re.sub(r"[èéêë]", "e", text)
    text = re.sub(r"[ìíîï]", "i", text)
    text = re.sub(r"[òóôõ]", "o", text)
    text = re.sub(r"[ùúûü]", "u", text)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def download_poster(url: str, slug: str) -> "str | None":
    """Download poster image, return relative path or None."""
    dest = POSTERS_DIR / f"{slug}.jpg"
    if dest.exists():
        return f"posters/{slug}.jpg"
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        dest.write_bytes(r.content)
        print(f"  ✓ poster {slug}.jpg")
        return f"posters/{slug}.jpg"
    except Exception as e:
        print(f"  ✗ poster download failed for {slug}: {e}")
        return None


def load_existing() -> dict:
    """Load existing JSON to allow incremental TMDB enrichment."""
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"movies": []}


def build_movie_index(existing: dict) -> dict:
    """Index existing movies by title (lowercased) for dedup."""
    return {m["title"].lower(): m for m in existing.get("movies", [])}


def merge_showtimes(existing: list, new: list) -> list:
    """Merge showtime lists, removing exact duplicates."""
    seen = {(s["cinema"], s["language"], s["date"], s["time"]) for s in existing}
    result = list(existing)
    for s in new:
        key = (s["cinema"], s["language"], s["date"], s["time"])
        if key not in seen:
            result.append(s)
            seen.add(key)
    return result


def _warn(message: str) -> None:
    """Print a warning both as plain text and as a GitHub Actions annotation."""
    print(f"  ⚠ WARNING: {message}")
    # GHA annotation — shows up in the workflow log and step summary
    print(f"::warning::{message}", flush=True)


def _write_step_summary(failed: list, scrapers: list) -> None:
    """Append a failure table to the GitHub Actions step summary if available."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    working = [cid for cid, _ in scrapers if cid not in failed]
    lines = [
        "## Scraper run summary\n",
        "| Cinema | Status |",
        "| --- | --- |",
    ]
    for cid in working:
        lines.append(f"| {cid} | ✅ OK |")
    for cid in failed:
        lines.append(f"| {cid} | ❌ 0 showtimes |")
    lines.append(f"\n> {len(failed)} scraper(s) need attention.\n")
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def run():
    print("── Cartelera Valencia Scraper ──")
    existing = load_existing()
    movie_index = build_movie_index(existing)

    scrapers = [
        ("babel",     scrape_babel),
        ("lys",       scrape_lys),
        ("abc",       scrape_abc),
        ("yelmo",     scrape_yelmo),
        ("kinepolis", scrape_kinepolis),
    ]

    # Raw showtimes: list of dicts with title, language (raw), cinema, date, time, url
    all_raw = []
    failed_cinemas: list[str] = []
    for cinema_id, scrape_fn in scrapers:
        print(f"\n[{cinema_id}] Scraping…")
        try:
            rows = scrape_fn()
            for r in rows:
                r["cinema"]   = cinema_id
                r["language"] = normalize_lang(r.get("language", ""))
            all_raw.extend(rows)
            if len(rows) == 0:
                _warn(f"{cinema_id}: returned 0 showtimes — scraper may be broken or selectors need updating")
                failed_cinemas.append(cinema_id)
            else:
                print(f"  → {len(rows)} showtimes")
        except Exception as e:
            _warn(f"{cinema_id}: scraper raised an exception — {e}")
            failed_cinemas.append(cinema_id)

    # Group by title
    by_title: dict[str, list] = {}
    for row in all_raw:
        t = row["title"].strip()
        by_title.setdefault(t, []).append(row)

    # Build final movie list
    final_movies = []
    for title, rows in by_title.items():
        title_lower = title.lower()
        existing_movie = movie_index.get(title_lower)

        showtimes = [
            {
                "cinema":   r["cinema"],
                "language": r["language"],
                "date":     r["date"],
                "time":     r["time"],
                "url":      r.get("url", "#"),
            }
            for r in rows
        ]

        if existing_movie:
            # Merge new showtimes into existing
            movie = dict(existing_movie)
            movie["showtimes"] = merge_showtimes(
                existing_movie.get("showtimes", []), showtimes
            )
        else:
            # New movie — enrich via TMDB
            print(f"\n[TMDB] Looking up: {title}")
            tmdb = enrich_movie(title)
            slug = slugify(title)

            poster_path = None
            if tmdb and tmdb.get("poster_url"):
                poster_path = download_poster(tmdb["poster_url"], slug)
                time.sleep(0.3)  # be polite to TMDB

            movie = {
                "id":              tmdb["id"] if tmdb else f"local-{slug}",
                "title":           title,
                "title_local":     tmdb.get("title_local", title) if tmdb else title,
                "poster":          poster_path,
                "rating":          tmdb.get("rating")  if tmdb else None,
                "duration":        tmdb.get("duration") if tmdb else None,
                "genres":          tmdb.get("genres", []) if tmdb else [],
                "trailer_youtube_id": tmdb.get("trailer_youtube_id") if tmdb else None,
                "showtimes":       showtimes,
            }

        final_movies.append(movie)

    # Sort by rating desc, unrated last
    final_movies.sort(
        key=lambda m: m.get("rating") or 0,
        reverse=True
    )

    output = {
        "updated_at": date.today().isoformat() + "T07:00:00Z",
        "movies":     final_movies,
    }

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    total_st = sum(len(m["showtimes"]) for m in final_movies)
    print(f"\n✓ {len(final_movies)} movies, {total_st} showtimes → {DATA_FILE}")

    if failed_cinemas:
        _warn(f"SUMMARY: {len(failed_cinemas)} cinema(s) produced no data: {', '.join(failed_cinemas)}")
        _write_step_summary(failed_cinemas, scrapers)
        sys.exit(1)


if __name__ == "__main__":
    run()
