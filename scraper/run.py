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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

# ── Cinema scrapers (one per file)
from babel     import scrape as scrape_babel
from lys       import scrape as scrape_lys
from cinema_abc import scrape_park, scrape_elsaler, scrape_granturia
from ocine          import scrape as scrape_ocine
from cinestudio_dor import scrape as scrape_dor
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
    # → VO  (original audio, no subtitles)
    "vo": "VO", "v.o.": "VO", "original": "VO",
    # → VOSE  (original audio + Spanish subtitles)
    "vose": "VOSE", "v.o.s.e.": "VOSE", "vos": "VOSE", "v.o.s.": "VOSE",
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


def has_tmdb_data(movie: dict) -> bool:
    return bool(movie.get("poster") or movie.get("rating") or movie.get("trailer_youtube_id"))


def build_movie_index(existing: dict) -> dict:
    """Index existing movies by title (lowercased) for dedup, merging duplicates."""
    index = {}
    for m in existing.get("movies", []):
        key = m["title"].lower()
        if key in index:
            # Prefer the entry that has TMDB data
            if has_tmdb_data(m) and not has_tmdb_data(index[key]):
                showtimes = merge_showtimes(m.get("showtimes", []), index[key].get("showtimes", []))
                index[key] = dict(m)
                index[key]["showtimes"] = showtimes
            else:
                index[key]["showtimes"] = merge_showtimes(
                    index[key].get("showtimes", []), m.get("showtimes", [])
                )
        else:
            index[key] = m
    return index


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


def run():
    print("── Cartelera Valencia Scraper ──")
    existing = load_existing()
    movie_index = build_movie_index(existing)

    scrapers = [
        ("babel",         scrape_babel),
        ("lys",           scrape_lys),
        ("abc_park",      scrape_park),
        ("abc_elsaler",   scrape_elsaler),
        ("abc_granturia", scrape_granturia),
        ("ocine",         scrape_ocine),
        ("dor",           scrape_dor),
        ("yelmo",         scrape_yelmo),
        ("kinepolis",     scrape_kinepolis),
    ]

    # Raw showtimes: list of dicts with title, language (raw), cinema, date, time, url
    all_raw = []
    scraper_errors = []

    def _run(cinema_id: str, scrape_fn) -> tuple[str, list, "Exception | None"]:
        print(f"[{cinema_id}] Scraping…")
        try:
            rows = scrape_fn()
            for r in rows:
                r["cinema"]   = cinema_id
                r["language"] = normalize_lang(r.get("language", ""))
            print(f"[{cinema_id}] → {len(rows)} showtimes")
            return cinema_id, rows, None
        except Exception as e:
            print(f"[{cinema_id}] ✗ Failed: {e}")
            return cinema_id, [], e

    with ThreadPoolExecutor(max_workers=len(scrapers)) as pool:
        futures = {pool.submit(_run, cid, fn): cid for cid, fn in scrapers}
        for future in as_completed(futures):
            cinema_id, rows, error = future.result()
            if error:
                scraper_errors.append((cinema_id, error))
            else:
                all_raw.extend(rows)

    if scraper_errors:
        raise RuntimeError(
            "Scraper failures — cannot produce reliable output:\n"
            + "\n".join(f"  {cid}: {err}" for cid, err in scraper_errors)
        )

    validate_per_cinema_per_day(all_raw)

    # Group by title (case-insensitive; preserve casing of first occurrence)
    by_title: dict[str, list] = {}
    title_canonical: dict[str, str] = {}
    for row in all_raw:
        t = row["title"].strip()
        key = t.lower()
        if key not in title_canonical:
            title_canonical[key] = t
        by_title.setdefault(key, []).append(row)

    # Build final movie list
    final_movies = []
    for title_key, rows in by_title.items():
        canonical = title_canonical[title_key]
        existing_movie = movie_index.get(title_key)

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

        if existing_movie and has_tmdb_data(existing_movie):
            # Merge new showtimes into existing
            movie = dict(existing_movie)
            movie["showtimes"] = merge_showtimes(
                existing_movie.get("showtimes", []), showtimes
            )
        else:
            # New movie or existing without TMDB data — enrich via TMDB
            print(f"\n[TMDB] Looking up: {canonical}")
            tmdb = enrich_movie(canonical)
            slug = slugify(canonical)

            poster_path = existing_movie.get("poster") if existing_movie else None
            if tmdb and tmdb.get("poster_url") and not poster_path:
                poster_path = download_poster(tmdb["poster_url"], slug)
                time.sleep(0.3)  # be polite to TMDB

            movie = {
                "id":              tmdb["id"] if tmdb else (existing_movie["id"] if existing_movie else f"local-{slug}"),
                "title":           canonical,
                "title_local":     tmdb.get("title_local", canonical) if tmdb else canonical,
                "poster":          poster_path,
                "rating":          tmdb.get("rating")  if tmdb else None,
                "duration":        tmdb.get("duration") if tmdb else None,
                "genres":          tmdb.get("genres", []) if tmdb else [],
                "trailer_youtube_id": tmdb.get("trailer_youtube_id") if tmdb else None,
                "showtimes":       merge_showtimes(existing_movie.get("showtimes", []), showtimes) if existing_movie else showtimes,
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


def validate_per_cinema_per_day(all_raw: list[dict]) -> None:
    """Raise if any cinema returns zero showtimes for any date in its expected window."""
    from ocine import _dates_until_next_thursday

    today = date.today()
    std_dates = {(today + timedelta(days=i)).isoformat() for i in range(7)}

    # Ocine runs on a Friday→Thursday cycle; its window may be shorter than 7 days.
    ocine_dates = set(_dates_until_next_thursday())

    expected: dict[str, set[str]] = {
        "babel":         std_dates,
        "lys":           std_dates,
        "abc_park":      std_dates,
        "abc_elsaler":   std_dates,
        "abc_granturia": std_dates,
        "ocine":         ocine_dates,
        "dor":           std_dates,
        "yelmo":         std_dates,
        "kinepolis":     std_dates,
    }

    covered: set[tuple[str, str]] = set()
    for row in all_raw:
        covered.add((row["cinema"], row["date"]))

    missing = []
    for cinema, dates in sorted(expected.items()):
        for d in sorted(dates):
            if (cinema, d) not in covered:
                missing.append(f"  {cinema} on {d}")

    if missing:
        raise RuntimeError(
            "Every cinema must have at least 1 movie for each expected day.\n"
            "Missing showtimes for:\n" + "\n".join(missing)
        )


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"\n✗ FATAL: {exc}", file=sys.stderr)
        sys.exit(1)
