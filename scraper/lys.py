"""
Cines Lys Valencia scraper.
URL: https://www.reservaentradas.com/cine/valencia/cineslys

Two-phase approach (mirrors babel.py):

Phase 1 (Playwright, cinema page):
  For each movie block collect title, language, and the sesiones page URL.
  The "Ver más" link (a.sesion.vtadanger) and the title link both point to
  the movie's /sesiones/ page on reservaentradas.com.

  DOM structure (server-rendered Bootstrap/jQuery):
    div.movie.row
      div.title-movie-list a   → title text + href → /sesiones/… URL
      span.label-cinema        → language label (VOSE / VO / empty = ES)
      a.sesion.vtadanger       → "Ver más" link → /sesiones/… URL (fallback)

Phase 2 (requests + BeautifulSoup, per-movie sesiones page):
  Fetch each movie's /sesiones/ page which lists sessions grouped by date.

  Date tabs: <li><a href="#N">Day DD/MM</a></li>
  Session sections: <div id="N"> containing <a href="/entrada/.../{id}/?step=2">HH:MM</a>
"""

import re
import requests
from datetime import date, timedelta
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE_URL    = "https://www.reservaentradas.com"
CINEMA_PATH = "/cine/valencia/cineslys"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}


def _parse_sesiones_date(tab_text: str) -> "str | None":
    """Parse 'Ju 23 / 04' or 'Ju23/04' → '2026-04-23'. Returns None if unparseable."""
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
            url = href if href.startswith("http") else BASE_URL + href
            result[(date_str, time_text)] = url

    return result


def scrape() -> list[dict]:
    results = []
    phase1: list[dict] = []  # {title, language, sesiones_url}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=_HEADERS["User-Agent"])

        try:
            page.goto(f"{BASE_URL}{CINEMA_PATH}", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)

            for block in page.query_selector_all("div.movie.row"):
                # Title
                title_el = block.query_selector("div.title-movie-list a")
                if not title_el:
                    continue
                title = title_el.inner_text().strip()
                if not title:
                    continue

                # Sesiones URL: prefer title link, fall back to vtadanger "Ver más"
                sesiones_url = ""
                href = title_el.get_attribute("href") or ""
                if href and "/sesiones/" in href:
                    sesiones_url = href if href.startswith("http") else BASE_URL + href

                if not sesiones_url:
                    danger_el = block.query_selector("a.sesion.vtadanger")
                    if danger_el:
                        href2 = danger_el.get_attribute("href") or ""
                        if href2 and "/sesiones/" in href2:
                            sesiones_url = href2 if href2.startswith("http") else BASE_URL + href2

                if not sesiones_url:
                    continue

                # Language from span.label-cinema (VOSE / VO / empty → ES)
                lang_el = block.query_selector("span.label-cinema")
                language = lang_el.inner_text().strip() if lang_el else "es"
                if not language:
                    language = "es"

                phase1.append({
                    "title":       title,
                    "language":    language,
                    "sesiones_url": sesiones_url,
                })

        except Exception as e:
            print(f"  ⚠ Lys error: {e}")

        browser.close()

    # Phase 2: fetch per-movie sesiones pages for multi-day sessions
    session_cache: dict[str, dict[tuple[str, str], str]] = {}
    for entry in phase1:
        url = entry["sesiones_url"]
        if url not in session_cache:
            session_cache[url] = _fetch_session_map(url)

    for entry in phase1:
        session_map = session_cache.get(entry["sesiones_url"], {})
        for (date_str, time_str), url in session_map.items():
            results.append({
                "title":    entry["title"],
                "language": entry["language"],
                "date":     date_str,
                "time":     time_str,
                "url":      url,
            })

    return results
