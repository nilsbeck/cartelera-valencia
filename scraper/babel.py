"""
Cine Babel Valencia scraper.
URL: https://cinesbabel.com

Returns list of dicts:
  { title, language, date, time, url }

Page structure (as of June 2026):
  div.peliculas > div.pelicula-post (one per movie)
    div.pelicula-content
      div.pelicula-title
        h2  → film title
        div "Idioma: ..."       → spoken language
        div "Subtítulos: ..."   → subtitle language
      div.pelicula-fechas > table.tabla-sesiones > tbody > tr
        td[0]  → date string, e.g. "Jue 25 Jun"
        td[1+] → <a href="...">HH:MM</a>  (may be empty)
"""

import re
from datetime import date
from typing import Optional
from playwright.sync_api import sync_playwright

BASE_URL = "https://cinesbabel.com"

# Spanish month abbreviations → month number
_MONTHS = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4,
    "may": 5, "jun": 6, "jul": 7, "ago": 8,
    "sep": 9, "oct": 10, "nov": 11, "dic": 12,
}


def _parse_date(date_str: str) -> Optional[str]:
    """Convert 'Jue 25 Jun' → '2026-06-25'. Returns None on parse failure."""
    parts = date_str.strip().split()
    if len(parts) < 3:
        return None
    try:
        day = int(parts[1])
        month = _MONTHS.get(parts[2].lower()[:3])
        if not month:
            return None
        today = date.today()
        year = today.year
        # If the month is before today's month it's next year
        if month < today.month:
            year += 1
        return date(year, month, day).isoformat()
    except (ValueError, IndexError):
        return None


def scrape() -> list[dict]:
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ))
        page.set_extra_http_headers({
            "Accept-Language": "es-ES,es;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })

        try:
            page.goto(f"{BASE_URL}/cartelera/", timeout=20000)
            page.wait_for_load_state("networkidle", timeout=15000)

            movie_blocks = page.query_selector_all(".pelicula-post")

            for block in movie_blocks:
                title_el = block.query_selector(".pelicula-title h2")
                if not title_el:
                    continue
                title = title_el.inner_text().strip()

                # Language: plain div text "Idioma: Catalán"
                language = "ES"
                lang_divs = block.query_selector_all(".pelicula-title div")
                for div in lang_divs:
                    text = div.inner_text().strip()
                    m = re.match(r"Idioma:\s*(.+)", text)
                    if m:
                        language = m.group(1).strip()
                        break

                # Each <tr> has date in td[0], times in td[1+]
                rows = block.query_selector_all(".tabla-sesiones tr")
                for row in rows:
                    cells = row.query_selector_all("td")
                    if not cells:
                        continue
                    date_str = _parse_date(cells[0].inner_text())
                    if not date_str:
                        continue

                    for cell in cells[1:]:
                        link = cell.query_selector("a")
                        if not link:
                            continue
                        time_text = link.inner_text().strip()
                        if not time_text:
                            continue
                        href = link.get_attribute("href") or BASE_URL
                        results.append({
                            "title":    title,
                            "language": language,
                            "date":     date_str,
                            "time":     time_text,
                            "url":      href if href.startswith("http") else BASE_URL + href,
                        })

        except Exception as e:
            print(f"  ⚠ Babel scrape error: {e}")

        browser.close()

    return results
