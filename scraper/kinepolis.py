"""
Kinépolis Valencia scraper.
URL: https://kinepolis.es/?complex=KVAL&main_section=ya+a+la+venta

The page (Drupal + Comato) embeds two JSON blobs in <script> tags:
  1. "sessions":[...]  — all sessions across all Kinépolis cinemas
  2. "films":[...]     — full film records

Strategy:
  - Fetch with requests (no Playwright — the data is in the HTML source)
  - Parse both JSON arrays
  - Filter sessions by complexOperator == "KVAL" and date in next 7 days
  - Join film title via film.id → films[].id
  - Derive language from rawSessionAttributes / sessionAttributes[].code

Session fields used:
  showtime             ISO datetime "2026-04-22T18:00:00+00:00"
  businessDay          ISO date of the cinema day
  vistaSessionId       int  (used in booking URL)
  rawSessionAttributes "2D,nosubt,Spanish" / "2D,VOSE,English" / …
  sessionAttributes[].code  "Spanish" | "English" | "VOSE" | "nosubt" | …
  film.id              matches films[].id

Language mapping:
  "VOSE" attr OR "Span Subt" in raw  → vose
  "English" in raw, no "Spanish"     → vo
  otherwise                           → es
"""

import json
import re
import requests
from datetime import date, timedelta

BASE_URL = "https://kinepolis.es"
COMPLEX  = "KVAL"


# ── helpers ──────────────────────────────────────────────────────────────────

def _extract_json_array(html: str, marker: str) -> list:
    """
    Find a JSON array starting at 'marker' in the HTML and return it parsed.
    Uses bracket-counting to find the matching ']' robustly.
    """
    idx = html.find(marker)
    if idx == -1:
        return []
    start = idx + len(marker) - 1   # points at '['
    depth = 0
    i = start
    while i < len(html):
        c = html[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                break
        elif c == '"':
            # Skip over string literals so brackets inside strings don't confuse us
            i += 1
            while i < len(html) and html[i] != '"':
                if html[i] == "\\":
                    i += 1  # skip escaped char
                i += 1
        i += 1
    try:
        return json.loads(html[start : i + 1])
    except (json.JSONDecodeError, ValueError):
        return []


def _detect_language(raw_attrs: str, session_attrs: list) -> str:
    """Map Kinépolis session attribute strings to our normalised language codes."""
    raw = raw_attrs.lower()
    codes = {a.get("code", "").lower() for a in session_attrs}

    if "vose" in codes or "vose" in raw:
        return "vose"
    if "span subt" in raw or "spanish subt" in raw or "spanish sub" in raw:
        return "vose"   # English audio + Spanish subtitles
    if "english" in raw and "spanish" not in raw:
        return "vo"
    return "es"


# ── main scrape ───────────────────────────────────────────────────────────────

def scrape() -> list[dict]:
    results = []

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
        "Referer": "https://kinepolis.es/",
    }

    try:
        r = requests.get(
            f"{BASE_URL}/?complex={COMPLEX}&main_section=ya+a+la+venta",
            headers=headers,
            timeout=20,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"  ⚠ Kinepolis fetch error: {e}")
        return results

    html = r.text

    # ── Parse embedded JSON blobs ──
    films_list    = _extract_json_array(html, '"films":[')
    sessions_list = _extract_json_array(html, '"sessions":[')

    if not films_list:
        print("  ⚠ Kinepolis: could not parse 'films' JSON — page structure may have changed")
        return results
    if not sessions_list:
        print("  ⚠ Kinepolis: could not parse 'sessions' JSON — page structure may have changed")
        return results

    # Index films by their id field for O(1) lookup
    films_by_id: dict[str, dict] = {f["id"]: f for f in films_list if "id" in f}

    # Valid date window: today + 6 days
    today       = date.today()
    valid_dates = {(today + timedelta(days=i)).isoformat() for i in range(7)}

    for sess in sessions_list:
        # Only Valencia
        if sess.get("complexOperator") != COMPLEX:
            continue
        # Only public screenings
        if not sess.get("isPublicScreening", True):
            continue

        showtime_str = sess.get("showtime", "")
        m = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})", showtime_str)
        if not m:
            continue
        date_str, time_str = m.group(1), m.group(2)

        if date_str not in valid_dates:
            continue

        # Resolve film title
        film_id = sess.get("film", {}).get("id", "")
        film    = films_by_id.get(film_id, {})
        title   = (film.get("name") or film.get("title") or "").strip()
        if not title:
            continue

        language = _detect_language(
            sess.get("rawSessionAttributes", ""),
            sess.get("sessionAttributes", []),
        )

        # Booking URL: per-session deep-link via vistaSessionId
        vista_id = sess.get("vistaSessionId")
        if vista_id:
            url = (
                f"{BASE_URL}/compra-de-entradas/?"
                f"vistaSessionId={vista_id}&complex={COMPLEX}"
            )
        else:
            url = f"{BASE_URL}/?complex={COMPLEX}&main_section=ya+a+la+venta"

        results.append({
            "title":    title,
            "language": language,
            "date":     date_str,
            "time":     time_str,
            "url":      url,
        })

    return results
