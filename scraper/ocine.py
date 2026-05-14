"""
Ocine Premium Aqua Valencia scraper.
URL: https://www.ocinepremiumaqua.es/

Two-phase approach (requests + BeautifulSoup — no browser required):

Phase 1 (main page):
  Discover currently-showing films with their detail-page URLs.
  Each film block (div.peli-item.element-item) contains a link to
  /film-{id}/p?{slug}= which is the per-film session page.

Phase 2 (per-film date pages):
  For each film URL × date (today → next Thursday):
    Fetch {film_url}&selectedDate={YYYY-MM-DD}
    Parse sessions from table.planificacions → tr.plans rows.
    Language is detected from tr class names and adjacent version labels.

Spanish cinemas programme on a Friday→Thursday weekly cycle, so sessions
are published from today through the upcoming Thursday.

Film page URL pattern:
  https://www.ocinepremiumaqua.es/film-{id}/p?{movie-slug}=&selectedDate=YYYY-MM-DD
"""

import re
import requests
from datetime import date, timedelta
from bs4 import BeautifulSoup

BASE_URL = "https://ocinepremiumaqua.es"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/136.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.google.es/",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

_FILM_LINK_RE = re.compile(r"/film-\d+/p")

# Ordered so longer/more-specific tokens are checked first.
# "v.o.s.e" and "subtitulad" must precede "v.o." and "original"
# so VOSE beats VO when both substrings are present.
_LANG_TOKENS: list[tuple[str, str]] = [
    ("vose",       "vose"),
    ("v.o.s.e",    "vose"),
    ("subtitulad", "vose"),
    ("v.o.",       "vo"),
    ("original",   "vo"),
    ("doblad",     "es"),
    ("castellano", "es"),
    ("español",    "es"),
]


def _dates_until_next_thursday() -> list[str]:
    """
    Return ISO date strings from today through the upcoming Thursday (inclusive).
    Covers the current Spanish cinema week (Friday → Thursday cycle).
    If today is Thursday, returns today through the following Thursday (8 days).
    """
    today = date.today()
    days_ahead = (3 - today.weekday()) % 7  # 3 = Thursday
    if days_ahead == 0:
        days_ahead = 7
    end = today + timedelta(days=days_ahead)
    result = []
    d = today
    while d <= end:
        result.append(d.isoformat())
        d += timedelta(days=1)
    return result


def _detect_lang(text: str) -> str:
    """Infer a raw language tag from a version-label string."""
    t = text.lower()
    for token, lang in _LANG_TOKENS:
        if token in t:
            return lang
    return "es"


def scrape() -> list[dict]:
    results = []
    dates = _dates_until_next_thursday()
    sess = requests.Session()
    sess.headers.update(_HEADERS)

    # ── Phase 1: discover film detail URLs ───────────────────────────
    try:
        r = sess.get(BASE_URL, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"  ⚠ Ocine: failed to load {BASE_URL}: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    film_entries: list[dict] = []

    for block in soup.find_all("div", class_=lambda c: c and "peli-item" in c and "element-item" in c):
        h4 = block.find("h4")
        if not h4:
            continue
        title = h4.get_text(strip=True)
        if not title:
            continue

        link = block.find("a", href=_FILM_LINK_RE) or h4.find("a", href=_FILM_LINK_RE)
        if not link:
            continue
        href = link.get("href", "")
        if not _FILM_LINK_RE.search(href):
            continue

        film_url = href if href.startswith("http") else BASE_URL + href
        film_url = re.sub(r"[&?]selectedDate=[^&]*", "", film_url).rstrip("?&")
        film_entries.append({"title": title, "url": film_url})

    if not film_entries:
        print(f"  ⚠ Ocine: no film blocks found — page snippet: {r.text[:800]}")
        return []

    # ── Phase 2: per-film, per-date session scraping ─────────────────
    for entry in film_entries:
        title    = entry["title"]
        film_url = entry["url"]
        sep      = "&" if "?" in film_url else "?"

        for date_str in dates:
            target = f"{film_url}{sep}selectedDate={date_str}"
            try:
                r2 = sess.get(target, timeout=20)
                r2.raise_for_status()
            except Exception:
                continue

            soup2 = BeautifulSoup(r2.text, "html.parser")

            for tr in soup2.find_all("tr", class_="plans"):
                cls = " ".join(tr.get("class") or [])

                # Language priority:
                # 1. Token in the tr's own class string
                # 2. A version-label element inside the tr
                # 3. Text of the immediately preceding sibling tr
                lang = _detect_lang(cls)

                if lang == "es":
                    label_el = tr.find(class_=lambda c: c and any(
                        x in " ".join(c) for x in
                        ["versio", "version", "idioma", "horasessio-titol", "film-version", "session-version"]
                    ))
                    if label_el:
                        lang = _detect_lang(label_el.get_text())

                if lang == "es":
                    prev = tr.find_previous_sibling("tr")
                    if prev:
                        lang = _detect_lang(prev.get_text())

                for btn in tr.find_all("button"):
                    time_text = btn.get_text(strip=True)
                    if not time_text or ":" not in time_text:
                        continue
                    results.append({
                        "title":    title,
                        "language": lang,
                        "date":     date_str,
                        "time":     time_text,
                        "url":      film_url,
                    })

    return results
