"""
Cines Babel Valencia scraper.
URL: https://cinesbabel.com/cartelera/

Phase 1 (requests + BeautifulSoup, cartelera page):
  Collect per-movie: title, language, and the movie's /sesiones/ URL.

  DOM structure (server-rendered WordPress):
    div.pelicula-post           → one movie
      h2                        → title
      div (text "Idioma: X")    → spoken language
      div (text "Subtítulos: X")→ subtitle language
      table.tabla-sesiones
        tr
          td[0]  → date label
          td[1+] a → href = /sesiones/... URL + time text

Phase 2 (requests + BeautifulSoup, per-movie sesiones page):
  For each movie, fetch its reservaentradas.com /sesiones/ page which lists
  all sessions grouped by date with session-specific /entrada/ booking links.

  Date tabs: <li><a href="#N">Day DD/MM</a></li>
  Session sections: <div id="N"> containing <a href="/entrada/.../{id}/?step=2">HH:MM</a>

  Falls back to movie-level /sesiones/ URL if a sesiones page cannot be fetched.
"""

import re
import requests
from datetime import date, timedelta
from bs4 import BeautifulSoup

BASE_URL     = "https://cinesbabel.com"
BOOKING_BASE = "https://www.reservaentradas.com"

_MONTHS = {
    "Ene": 1, "Feb": 2, "Mar": 3, "Abr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Ago": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dic": 12,
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/136.0 Safari/537.36"
    ),
}


def _parse_cartelera_date(label: str) -> "str | None":
    """Convert 'Mié 22 Abr' → '2026-04-22'. Returns None if unparseable."""
    parts = label.split()
    if len(parts) < 3:
        return None
    try:
        day = int(parts[1])
        month = _MONTHS.get(parts[2].capitalize())
        if not month:
            return None
        today = date.today()
        year = today.year
        target = date(year, month, day)
        if target < today - timedelta(days=1):
            target = date(year + 1, month, day)
        return target.isoformat()
    except (ValueError, TypeError):
        return None


# Alias kept for backward-compat with test imports
_parse_date = _parse_cartelera_date


def _parse_sesiones_date(tab_text: str) -> "str | None":
    """Parse 'Ju 23 / 04' or 'Ju23/04' → '2026-04-23'."""
    m = re.search(r"(\d{1,2})\s*/\s*(\d{2})", tab_text)
    if not m:
        return None
    try:
        day, month = int(m.group(1)), int(m.group(2))
        today = date.today()
        year = today.year
        target = date(year, month, day)
        if target < today - timedelta(days=1):
            target = date(year + 1, month, day)
        return target.isoformat()
    except (ValueError, TypeError):
        return None


def _detect_language_texts(texts: list) -> str:
    """Core language detection logic shared between Playwright and BS4 paths."""
    idioma = ""
    subtitulos = ""
    for text in texts:
        if text.startswith("Idioma:"):
            idioma = text.split(":", 1)[1].strip().lower()
        elif text.startswith("Subtítulos:"):
            subtitulos = text.split(":", 1)[1].strip().lower()
    if subtitulos and subtitulos not in ("", "no"):
        return "vose"
    if idioma in ("español", "castellano", ""):
        return "es"
    return "vo"


def _detect_language(block) -> str:
    """Playwright-style block interface; kept for test compatibility."""
    texts = [el.inner_text().strip() for el in block.query_selector_all("div")]
    return _detect_language_texts(texts)


def _fetch_session_map(sesiones_url: str) -> "dict[tuple[str,str], str]":
    """
    Fetch a /sesiones/ page and return {(date_str, time_str): booking_url}.
    Returns empty dict on failure.
    """
    try:
        r = requests.get(sesiones_url, headers=_HEADERS, timeout=15)
        r.raise_for_status()
    except Exception:
        return {}

    soup = BeautifulSoup(r.text, "html.parser")
    result: dict[tuple[str, str], str] = {}

    # Date tabs: <li><a href="#1">...</a></li>
    tab_links = soup.find_all("a", href=re.compile(r"^#\d+$"))
    for tab in tab_links:
        section_id = tab["href"].lstrip("#")
        date_str = _parse_sesiones_date(tab.get_text())
        if not date_str:
            continue

        section = soup.find(id=section_id)
        if not section:
            continue

        for a in section.find_all("a", href=re.compile(r"/entrada/")):
            time_text = a.get_text(strip=True)
            if ":" not in time_text:
                continue
            href = a["href"]
            url = href if href.startswith("http") else BOOKING_BASE + href
            result[(date_str, time_text)] = url

    return result


def scrape() -> list[dict]:
    results = []
    phase1: list[dict] = []

    r = requests.get(f"{BASE_URL}/cartelera/", headers=_HEADERS, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    for block in soup.find_all("div", class_="pelicula-post"):
        h2 = block.find("h2")
        if not h2:
            continue
        title = h2.get_text(strip=True)
        if not title:
            continue

        language = _detect_language_texts(
            [div.get_text(strip=True) for div in block.find_all("div")]
        )

        table = block.find("table", class_="tabla-sesiones")
        if not table:
            continue

        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 2:
                continue

            date_str = _parse_cartelera_date(cells[0].get_text(strip=True))
            if not date_str:
                continue

            for cell in cells[1:]:
                a_el = cell.find("a")
                if not a_el:
                    continue
                time_text = a_el.get_text(strip=True)
                href = a_el.get("href") or ""
                if not time_text or ":" not in time_text:
                    continue
                sesiones_url = href if href.startswith("http") else BASE_URL + href
                phase1.append({
                    "title":        title,
                    "language":     language,
                    "date":         date_str,
                    "time":         time_text,
                    "sesiones_url": sesiones_url,
                })

    # Phase 2: upgrade movie-level URLs to session-specific booking URLs
    session_cache: dict[str, dict[tuple[str, str], str]] = {}
    for entry in phase1:
        url = entry["sesiones_url"]
        if url not in session_cache:
            session_cache[url] = _fetch_session_map(url)

    for entry in phase1:
        session_map = session_cache.get(entry["sesiones_url"], {})
        url = session_map.get(
            (entry["date"], entry["time"]),
            entry["sesiones_url"],  # fallback to movie-level sesiones URL
        )
        results.append({
            "title":    entry["title"],
            "language": entry["language"],
            "date":     entry["date"],
            "time":     entry["time"],
            "url":      url,
        })

    return results
