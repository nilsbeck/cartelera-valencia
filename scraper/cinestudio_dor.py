"""
Cinestudio d'Or Valencia scraper.
URL: https://www.cinestudiodor.es/

The site is a Blogger blog where each post = one currently-showing film.
Structure (static HTML — requests, no Playwright needed):

  div.post-outer                    → one film post
    h3 a                            → title (h3 is display:none)
    div.separator > b               → date range  e.g. "20 — 26 abril"
                                      or "27 abril — 3 mayo"
    span > span[font-size:medium]   → times       e.g. "16:30h. 20:30h."
    span[font-size:x-small]         → language    e.g. "versión doblada / digital"
                                                        "versión original / subtítulos en castellano"

Booking URL: https://www.reservaentradas.com/cine/valencia/cinestudiodor
(all sessions share the same booking page)

Date handling:
  - Parse the date-range string into a list of individual dates.
  - Only emit showtimes for dates within today + 6 days.
  - Times in parentheses like "(L 18:05h.)" are day-specific exceptions and
    are intentionally ignored (they vary by day of week, not tracked here).
"""

import re
import time
import requests
from bs4 import BeautifulSoup
from datetime import date, timedelta

BASE_URL    = "https://www.cinestudiodor.es/"
BOOKING_URL = "https://www.reservaentradas.com/cine/valencia/cinestudiodor"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/136.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.google.es/",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

_MONTHS_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}


# ── Date-range parser ─────────────────────────────────────────────────────────

def _parse_date_part(text: str, fallback_month: int, fallback_year: int) -> "date | None":
    """Parse a date fragment like '20', '20 abril', '27 abril' into a date."""
    tokens = text.strip().lower().split()
    if not tokens:
        return None
    try:
        day = int(tokens[0])
    except ValueError:
        return None
    month = _MONTHS_ES.get(tokens[1]) if len(tokens) > 1 else fallback_month
    if not month:
        return None
    try:
        return date(fallback_year, month, day)
    except ValueError:
        return None


def _parse_date_range(text: str) -> list[date]:
    """
    Convert 'DD [month] — DD month' into a list of date objects.
    Handles same-month ("20 — 26 abril") and cross-month ("27 abril — 3 mayo").
    """
    parts = [p.strip() for p in re.split(r"—|-", text.lower())]
    if len(parts) != 2:
        return []

    today = date.today()
    year = today.year

    # End part always carries a month name
    end_tokens = parts[1].split()
    end_month = next((v for k, v in _MONTHS_ES.items() if k in end_tokens), None)
    if not end_month:
        return []

    end = _parse_date_part(parts[1], end_month, year)
    if not end:
        return []

    # If end is in the past by more than a week it must be next year
    if end < today - timedelta(days=7):
        end = date(year + 1, end.month, end.day)
        year = year + 1

    # Start part: may or may not carry a month name; fall back to end month
    start_tokens = parts[0].split()
    start_month = next((v for k, v in _MONTHS_ES.items() if k in start_tokens), end_month)
    start = _parse_date_part(parts[0], start_month, year)
    if not start:
        return []

    # Cross-year edge: start after end means start is in prior year
    if start > end:
        start = date(year - 1, start.month, start.day)

    dates = []
    current = start
    while current <= end:
        dates.append(current)
        current += timedelta(days=1)
    return dates


# ── Time parser ───────────────────────────────────────────────────────────────

def _parse_times(text: str) -> list[str]:
    """Extract 'HH:MM' strings, ignoring day-specific parentheticals."""
    clean = re.sub(r"\([^)]*\)", "", text)   # strip "(L 18:05h.)" etc.
    return re.findall(r"\d{1,2}:\d{2}", clean)


# ── Language detector ─────────────────────────────────────────────────────────

def _detect_language(text: str) -> str:
    t = text.lower()
    if "doblad" in t or "versión española" in t or "v.esp" in t:
        return "es"
    has_subs = ("subtítulo" in t or "subtitulo" in t) and "sin subtítulo" not in t and "sin subtitulo" not in t
    if has_subs or "vose" in t:
        return "vose"
    if "original" in t or "v.o." in t:
        return "vo"
    return "es"


# ── Main scrape ───────────────────────────────────────────────────────────────

def scrape() -> list[dict]:
    results = []
    today = date.today()
    valid_dates = {(today + timedelta(days=i)).isoformat() for i in range(7)}

    resp = None
    for attempt, delay in enumerate([0, 2, 4, 8]):
        if delay:
            time.sleep(delay)
        try:
            resp = requests.get(BASE_URL, headers=_HEADERS, timeout=20)
            resp.raise_for_status()
            break
        except Exception as e:
            print(f"  ⚠ Cinestudio d'Or fetch error (attempt {attempt + 1}): {e}")
            resp = None
    if resp is None:
        return results

    soup = BeautifulSoup(resp.text, "html.parser")

    for post in soup.select("div.post-outer"):
        # Title (inside hidden h3)
        h3_a = post.select_one("h3 a")
        if not h3_a:
            continue
        title = h3_a.get_text(strip=True)
        if not title:
            continue

        # Date range: first <b> inside a div.separator
        b_el = post.select_one("div.separator b")
        if not b_el:
            continue
        date_range_text = b_el.get_text(strip=True)
        post_dates = _parse_date_range(date_range_text)
        valid_post_dates = [d for d in post_dates if d.isoformat() in valid_dates]
        if not valid_post_dates:
            continue

        # Times: look for a span containing a span with font-size:medium
        times: list[str] = []
        for span in post.find_all("span", style=lambda s: s and "medium" in s):
            times = _parse_times(span.get_text())
            if times:
                break

        if not times:
            continue

        # Language: span with font-size:x-small
        language = "es"
        for span in post.find_all("span", style=lambda s: s and "x-small" in s):
            lang_text = span.get_text(strip=True)
            if lang_text:
                language = _detect_language(lang_text)
                break

        for d in valid_post_dates:
            for t in times:
                results.append({
                    "title":    title,
                    "language": language,
                    "date":     d.isoformat(),
                    "time":     t,
                    "url":      BOOKING_URL,
                })

    return results
