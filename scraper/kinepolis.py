"""
Kinépolis Valencia scraper.
URL: https://kinepolis.es/?complex=KVAL&main_section=ya+a+la+venta

The page (Drupal + Comato) embeds two JSON blobs in <script> tags:
  1. "sessions":[...]  — all sessions across all Kinépolis cinemas
  2. "films":[...]     — full film records

Strategy:
  - Fetch with curl_cffi impersonating Chrome (no Playwright — the data is
    in the HTML source). The origin sits behind a WAF that fingerprints the
    TLS/HTTP2 handshake and 403s plain-requests traffic, so a spoofed
    User-Agent alone is not enough — the handshake itself must look like
    Chrome's.
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
import unicodedata
from datetime import date, timedelta

from curl_cffi import requests

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


def _slugify(text: str) -> str:
    """URL slug in Kinepolis format: lowercase, ASCII, non-alphanumerics → '-'."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text


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

def scrape() -> tuple[list[dict], list[str]]:
    results: list[dict] = []
    diags:   list[str]  = []

    # User-Agent and Accept are left to impersonate="chrome" so the UA string
    # matches the spoofed TLS fingerprint; a hardcoded UA version would
    # contradict the handshake and re-trip the WAF.
    headers = {
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Referer": "https://kinepolis.es/",
    }

    def _diag(msg: str) -> None:
        print(f"  [kinepolis] {msg}", flush=True)
        diags.append(msg)

    try:
        r = requests.get(
            f"{BASE_URL}/?complex={COMPLEX}&main_section=ya+a+la+venta",
            headers=headers,
            timeout=20,
            impersonate="chrome",
        )
    except Exception as e:
        _diag(f"network error: {e}")
        return results, diags

    _diag(f"HTTP {r.status_code}, body {len(r.text)} bytes")
    if r.status_code != 200:
        _diag(f"blocked: {r.text[:120].strip()!r}")
        return results, diags

    html = r.text

    # ── Parse embedded JSON blobs ──
    films_list    = _extract_json_array(html, '"films":[')
    sessions_list = _extract_json_array(html, '"sessions":[')

    if not films_list:
        _diag("'films' marker not found — page structure may have changed")
        return results, diags
    if not sessions_list:
        _diag("'sessions' marker not found — page structure may have changed")
        return results, diags

    _diag(f"parsed {len(films_list)} films, {len(sessions_list)} total sessions")

    # Index films by their id field for O(1) lookup
    films_by_id: dict[str, dict] = {f["id"]: f for f in films_list if "id" in f}

    # Valid date window: today + 6 days
    today       = date.today()
    valid_dates = {(today + timedelta(days=i)).isoformat() for i in range(7)}

    kval_total = 0
    for sess in sessions_list:
        # Only Valencia
        if sess.get("complexOperator") != COMPLEX:
            continue
        kval_total += 1
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
        has_vose_prefix = bool(re.match(r"^VOSE[:\s]", title, flags=re.IGNORECASE))
        title   = re.sub(r"^VOSE:\s*", "", title, flags=re.IGNORECASE).strip()
        title   = re.sub(r"^VOSE\s+", "", title, flags=re.IGNORECASE).strip()
        if not title:
            continue

        language = _detect_language(
            sess.get("rawSessionAttributes", ""),
            sess.get("sessionAttributes", []),
        )
        if has_vose_prefix and language == "es":
            language = "vose"

        # Movie detail URL: /movies/detail/{corporateId}/{id}/0/{slug}
        # (e.g. /movies/detail/36596/HO00006362/0/la-momia-de-lee-cronin)
        corporate_id = film.get("corporateId")
        film_ho_id   = film.get("id", "")
        film_name    = (film.get("name") or film.get("title") or "").strip()
        # Strip VOSE prefix so the slug matches the public URL
        clean_name   = re.sub(r"^VOSE[:\s]+", "", film_name, flags=re.IGNORECASE).strip()
        slug         = _slugify(clean_name)
        if corporate_id and film_ho_id and slug:
            url = f"{BASE_URL}/movies/detail/{corporate_id}/{film_ho_id}/0/{slug}"
        else:
            url = f"{BASE_URL}/?complex={COMPLEX}&main_section=ya+a+la+venta"

        results.append({
            "title":    title,
            "language": language,
            "date":     date_str,
            "time":     time_str,
            "url":      url,
        })

    if kval_total == 0:
        _diag(
            f"{len(sessions_list)} total sessions parsed but none for "
            f"complex={COMPLEX!r} — new week not published yet?"
        )
    elif not results:
        date_range = f"{min(valid_dates)} – {max(valid_dates)}"
        kval_dates = sorted({
            re.match(r"(\d{4}-\d{2}-\d{2})", s.get("showtime", "")).group(1)
            for s in sessions_list
            if s.get("complexOperator") == COMPLEX
            and re.match(r"(\d{4}-\d{2}-\d{2})", s.get("showtime", ""))
        })
        _diag(
            f"{kval_total} KVAL sessions found but none in window {date_range}; "
            f"KVAL dates on site: {kval_dates}"
        )

    return results, diags
