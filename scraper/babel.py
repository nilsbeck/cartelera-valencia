"""
Cines Babel Valencia scraper.
URL: https://cinesbabel.com/cartelera/

DOM structure (server-rendered WordPress):
  div.pelicula-post           → one movie
    h2                        → title
    div (text "Idioma: X")    → spoken language
    div (text "Subtítulos: X")→ subtitle language
    table.tabla-sesiones
      tr
        td[0]  → date label "Mié 22 Abr"
        td[1+] a → showtime links (no class, target="_blank")

All 7 days appear on a single page load — no need to iterate by date.
"""

from datetime import date, timedelta
from playwright.sync_api import sync_playwright

BASE_URL = "https://cinesbabel.com"

# Spanish short month names used in date labels like "Mié 22 Abr"
_MONTHS = {
    "Ene": 1, "Feb": 2, "Mar": 3, "Abr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Ago": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dic": 12,
}


def _parse_date(label: str) -> "str | None":
    """Convert 'Mié 22 Abr' → '2026-04-22'. Returns None if unparseable."""
    parts = label.split()          # ["Mié", "22", "Abr"]
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
        # Roll over to next year if the date is in the past (shouldn't happen,
        # but guard against Dec→Jan edge case)
        if target < today - timedelta(days=1):
            target = date(year + 1, month, day)
        return target.isoformat()
    except (ValueError, TypeError):
        return None


def _detect_language(block) -> str:
    """
    Read plain-text divs like 'Idioma: Inglés' / 'Subtítulos: Castellano'
    and return a normalised label for run.py's normalize_lang().
    """
    idioma = ""
    subtitulos = ""
    for div in block.query_selector_all("div"):
        text = div.inner_text().strip()
        if text.startswith("Idioma:"):
            idioma = text.split(":", 1)[1].strip().lower()
        elif text.startswith("Subtítulos:"):
            subtitulos = text.split(":", 1)[1].strip().lower()

    if subtitulos and subtitulos not in ("", "no"):
        return "vose"      # original audio + subtitles
    if idioma in ("español", "castellano", ""):
        return "es"
    return "vo"            # foreign language, no subtitles shown


def scrape() -> list[dict]:
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ))

        try:
            # Single page load — all 7 days are present in the HTML
            page.goto(f"{BASE_URL}/cartelera/", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)

            movie_blocks = page.query_selector_all("div.pelicula-post")

            for block in movie_blocks:
                title_el = block.query_selector("h2")
                if not title_el:
                    continue
                title = title_el.inner_text().strip()
                if not title:
                    continue

                language = _detect_language(block)

                # Each <tr> in tabla-sesiones: td[0]=date, td[1+] contain <a>
                for row in block.query_selector_all("table.tabla-sesiones tr"):
                    cells = row.query_selector_all("td")
                    if len(cells) < 2:
                        continue

                    date_str = _parse_date(cells[0].inner_text().strip())
                    if not date_str:
                        continue

                    for cell in cells[1:]:
                        a_el = cell.query_selector("a")
                        if not a_el:
                            continue
                        time_text = a_el.inner_text().strip()
                        href = a_el.get_attribute("href") or ""
                        if not time_text or ":" not in time_text:
                            continue
                        results.append({
                            "title":    title,
                            "language": language,
                            "date":     date_str,
                            "time":     time_text,
                            "url":      href if href.startswith("http") else BASE_URL + href,
                        })

        except Exception as e:
            print(f"  ⚠ Babel error: {e}")

        browser.close()

    return results
