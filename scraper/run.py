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
from mn4       import scrape as scrape_mn4
from cinema_abc import scrape_park, scrape_elsaler, scrape_granturia
from cinestudio_dor import scrape as scrape_dor
from ocine     import scrape as scrape_ocine, _dates_until_next_thursday
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
    cleaned = raw.strip().lower()
    if cleaned in LANG_MAP:
        return LANG_MAP[cleaned]
    # Substring fallback for verbose labels like "2D INGLÉS SUBTITULADO EN ESPAÑOL (VOSE)"
    if "vose" in cleaned or "v.o.s.e" in cleaned:
        return "VOSE"
    if "valencià" in cleaned or "valenciano" in cleaned:
        return "VAL"
    if "v.o." in cleaned or "original" in cleaned:
        return "VO"
    if "castellano" in cleaned or "español" in cleaned or "doblad" in cleaned:
        return "ES"
    return "ES"


def normalize_title_key(title: str) -> str:
    """Produce a dedup key from a movie title.

    Strips year annotations like (1986), normalizes separators, and lowercases
    so that 'TOP GUN (1986) - 40 ANIVERSARIO' and 'Top Gun 40 Aniversario'
    produce the same key while distinct films (e.g. 'Top Gun' vs
    'Top Gun: Maverick') remain separate.
    """
    t = title.strip().lower()
    t = re.sub(r"\(\d{4}\)", "", t)       # remove year: (1986)
    t = re.sub(r"[:\-·,|]+", " ", t)     # separators → space
    t = re.sub(r"\s+", " ", t).strip()
    return t


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
    """Index existing movies by normalized title for dedup, merging duplicates."""
    index = {}
    for m in existing.get("movies", []):
        key = normalize_title_key(m["title"])
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

    # Scrapers that run individually in parallel
    solo_scrapers = [
        ("babel",     scrape_babel),
        ("lys",       scrape_lys),
        ("mn4",       scrape_mn4),
        ("dor",       scrape_dor),
        ("ocine",     scrape_ocine),
        ("yelmo",     scrape_yelmo),
        ("kinepolis", scrape_kinepolis),
    ]
    # ABC cinemas share cinesabc.com — run them sequentially as one pool task
    abc_scrapers = [
        ("abc_park",      scrape_park),
        ("abc_elsaler",   scrape_elsaler),
        ("abc_granturia", scrape_granturia),
    ]

    # Raw showtimes: list of dicts with title, language (raw), cinema, date, time, url
    all_raw = []
    scraper_errors = []

    def _run(cinema_id: str, scrape_fn) -> list[tuple]:
        print(f"[{cinema_id}] Scraping…")
        try:
            rows = scrape_fn()
            for r in rows:
                r["cinema"]   = cinema_id
                r["language"] = normalize_lang(r.get("language", ""))
            print(f"[{cinema_id}] → {len(rows)} showtimes")
            return [(cinema_id, rows, None)]
        except Exception as e:
            print(f"[{cinema_id}] ✗ Failed: {e}")
            return [(cinema_id, [], e)]

    def _run_abc() -> list[tuple]:
        results = []
        for cinema_id, scrape_fn in abc_scrapers:
            results.extend(_run(cinema_id, scrape_fn))
        return results

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_run, cid, fn) for cid, fn in solo_scrapers]
        futures.append(pool.submit(_run_abc))
        for future in as_completed(futures):
            for cinema_id, rows, error in future.result():
                if error:
                    scraper_errors.append((cinema_id, error))
                else:
                    all_raw.extend(rows)

    if scraper_errors:
        for cid, err in scraper_errors:
            print(f"  ⚠ Scraper failed: {cid}: {err}")

    warnings = [f"Scraper failed — {cid}: {err}" for cid, err in scraper_errors]
    warnings += validate_per_cinema_per_day(all_raw)

    # Group by normalized title; preserve casing of first occurrence
    by_title: dict[str, list] = {}
    title_canonical: dict[str, str] = {}
    for row in all_raw:
        t = row["title"].strip()
        key = normalize_title_key(t)
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

    # Build the summary the notifier sends out. Kept here (not in main)
    # so the per-cinema counts come from the same all_raw that drove the
    # validation logic — no risk of the body lying about coverage.
    cinema_counts: dict[str, int] = {}
    for row in all_raw:
        cinema_counts[row["cinema"]] = cinema_counts.get(row["cinema"], 0) + 1

    summary_lines = [
        f"Movies:    {len(final_movies)}",
        f"Showtimes: {total_st}",
        "",
        "Per cinema:",
    ]
    for cid in sorted(cinema_counts):
        summary_lines.append(f"  • {cid}: {cinema_counts[cid]}")
    summary = "\n".join(summary_lines)

    return warnings, summary


def validate_per_cinema_per_day(all_raw: list[dict]) -> list[str]:
    """Return warning strings for any cinema missing showtimes on an expected day."""
    today = date.today()
    std_dates = {(today + timedelta(days=i)).isoformat() for i in range(7)}
    # Lys and MN4 multi-day data comes from reservaentradas.com /sesiones/
    # pages, which drop past sessions during the day. By late afternoon today
    # may legitimately be absent, so only require the next 6 days.
    future_only = std_dates - {today.isoformat()}

    # Ocine runs on a Friday→Thursday cycle; its window may be shorter than 7 days.
    ocine_dates = set(_dates_until_next_thursday())

    expected: dict[str, set[str]] = {
        "babel":         std_dates,
        "lys":           future_only,
        "mn4":           future_only,
        "abc_park":      std_dates,
        "abc_elsaler":   std_dates,
        "abc_granturia": std_dates,
        "dor":           std_dates,
        "ocine":         ocine_dates,
        "yelmo":         std_dates,
        "kinepolis":     std_dates,
    }

    covered: set[tuple[str, str]] = set()
    for row in all_raw:
        covered.add((row["cinema"], row["date"]))

    warnings = []
    for cinema, dates in sorted(expected.items()):
        for d in sorted(dates):
            if (cinema, d) not in covered:
                warnings.append(f"{cinema} missing on {d}")

    return warnings


if __name__ == "__main__":
    import traceback
    from notify import notify

    # Exit codes:
    #   0 = clean run, no warnings.
    #   1 = scrape produced warnings (a cinema returned 0 sessions for a day,
    #       one scraper raised, etc.) — data on disk is still valid and
    #       should be committed.
    #   2 = fatal: run() itself raised. Don't commit, the dataset may be
    #       partial or absent.
    try:
        warnings, summary = run()
    except Exception as exc:
        notify(
            title="Cartelera Valencia: ✗ FATAL",
            body=f"{exc}\n\n{traceback.format_exc()}",
            warning=True,
        )
        print(f"\n✗ FATAL: {exc}", file=sys.stderr)
        sys.exit(2)
    if warnings:
        body = summary + "\n\nWarnings:\n" + "\n".join(f"  • {w}" for w in warnings)
        notify(
            title=f"Cartelera Valencia: ⚠ {len(warnings)} warning(s)",
            body=body,
            warning=True,
        )
        print("\n⚠ Completed with warnings:", file=sys.stderr)
        for w in warnings:
            print(f"  {w}", file=sys.stderr)
        sys.exit(1)

    notify(
        title="Cartelera Valencia: ✓ clean run",
        body=summary,
        warning=False,
    )
