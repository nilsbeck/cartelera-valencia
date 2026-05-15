"""
TMDB enrichment — looks up a movie title and returns
poster URL, rating, duration, genres, trailer YouTube ID.

Requires env var: TMDB_API_KEY
"""

import os
import re
import requests

TMDB_KEY  = os.environ.get("TMDB_API_KEY", "")
BASE_URL  = "https://api.themoviedb.org/3"
IMG_BASE  = "https://image.tmdb.org/t/p/w342"  # 342px wide poster
HEADERS   = {"accept": "application/json"}
LANG      = "es-ES"   # prefer Spanish metadata


# ── Title cleaning ────────────────────────────────────────────────────────────
#
# Cinema schedules are full of programmed-event titles like
#   "Cine con coloquio – Iron Maiden: Burning ambition"
#   "EL PADRINO (50 ANIVERSARIO)"
#   "EL SEÑOR DE LOS ANILLOS: LA COMUNIDAD DEL ANILLO-VERSIÓN EXTENDIDA"
#   "TOP GUN: MAVERICK REESTRENO"
# TMDB will never match the raw string, but does match the cleaned film name.

_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"30\s+a[nñ]os\s+30\s+pel[ií]culas"
    r"|cine\s+con\s+coloquio"
    r"|cine\s+con\s+cineastas"
    r"|cine\s+familiar"
    r"|cine\s+club"
    r"|club\s+de\s+lectura(?:\s+\w+)*"
    r"|producci[óo]n\s+valenciana"
    r"|presentaci[óo]n"
    r"|docs\s+vlc\d+"
    r"|pelos\s+de\s+punta"
    r")\s*[:\-–—]\s*",
    re.IGNORECASE,
)

_SUFFIX_PATTERNS = [
    re.compile(r"\s*[-–—]\s*pase\s+especial\s+con\s+coloquio\s*$", re.IGNORECASE),
    re.compile(r"\s*[-–—]\s*club\s+rosebud\s*$",                   re.IGNORECASE),
    re.compile(r"\s*[-–—]?\s+(?:re|pre)?estreno\s*$",              re.IGNORECASE),
    re.compile(r"\s*\(\s*\d+\s*[°ºo]?\s*aniversario\s*\)\s*$",     re.IGNORECASE),
    re.compile(r"\s+\d+\s*[°ºo]?\s*aniversario\s*$",               re.IGNORECASE),
    re.compile(r"\s*[-–—]?\s*\(\s*versi[óo]n\s+extendida\s*\)\s*$", re.IGNORECASE),
    re.compile(r"\s*[-–—]\s*versi[óo]n\s+extendida\s*$",           re.IGNORECASE),
    re.compile(r"\s+\d{1,2}/\d{1,2}\s*$"),
    re.compile(r"\s*\(\s*\d{4}\s*\)\s*$"),
]

# Last-ditch: anything inside trailing parens. Run only as final fallback —
# many films legitimately carry a parenthetical (e.g. "In the mood for love
# (Deseando amar)") that's actually useful for the first attempt.
_TRAILING_PARENS = re.compile(r"\s*\([^)]*\)\s*$")


def _clean_title(title: str) -> str:
    """Strip programmed-event prefix and event/anniversary suffix."""
    t = (title or "").strip()
    if not t:
        return t
    m = _PREFIX_RE.match(t)
    if m:
        t = t[m.end():].strip()
    while True:
        before = t
        for p in _SUFFIX_PATTERNS:
            t = p.sub("", t)
        t = t.rstrip(" -–—:")
        if t == before:
            break
    return t


def _search_candidates(title: str) -> list[str]:
    """Title variants to try against TMDB, most-faithful first."""
    seen: list[str] = []

    def _add(s: str) -> None:
        s = s.strip()
        if s and s not in seen:
            seen.append(s)

    _add(title)
    cleaned = _clean_title(title)
    _add(cleaned)
    # Final fallback: also drop any trailing parenthetical from the cleaned form.
    _add(_TRAILING_PARENS.sub("", cleaned))
    return seen


# ── TMDB search ───────────────────────────────────────────────────────────────

def _tmdb_search(query: str) -> list[dict]:
    r = requests.get(
        f"{BASE_URL}/search/movie",
        params={"api_key": TMDB_KEY, "query": query, "language": LANG},
        headers=HEADERS,
        timeout=8,
    )
    r.raise_for_status()
    return r.json().get("results", [])


def enrich_movie(title: str) -> "dict | None":
    if not TMDB_KEY:
        print("  ⚠ TMDB_API_KEY not set, skipping enrichment")
        return None

    results: list[dict] = []
    matched_query = title
    for candidate in _search_candidates(title):
        try:
            results = _tmdb_search(candidate)
        except Exception as e:
            print(f"  ✗ TMDB search failed for {candidate!r}: {e}")
            return None
        if results:
            matched_query = candidate
            if candidate != title:
                print(f"  ↻ Retried as: {candidate}")
            break

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
