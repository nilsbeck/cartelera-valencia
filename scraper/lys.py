"""
Cines Lys Valencia scraper.
URL: https://www.reservaentradas.com/cine/valencia/cineslys

DOM structure (server-rendered Bootstrap/jQuery):
  div.list-movies
    div.movie.row                 → one movie
      div.title-movie-list a      → title (text of the anchor)
      span.label-cinema           → language label (VOSE / VO / ES / empty)
      a.sesion.vtasuccess         → showtime link  ← use these
      a.sesion.vtadanger          → "Ver más" link ← skip (not a time)

The page only shows TODAY's sessions under "Sesiones para hoy".
All scraped showtimes are tagged with today's date.

Booking href pattern:
  /entrada/valencia/cineslys/{movie-slug}/{session-id}/?step=2
"""

from datetime import date
from playwright.sync_api import sync_playwright

BASE_URL    = "https://www.reservaentradas.com"
CINEMA_PATH = "/cine/valencia/cineslys"


def scrape() -> list[dict]:
    results = []
    today = date.today().isoformat()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ))

        try:
            page.goto(f"{BASE_URL}{CINEMA_PATH}", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)

            movie_blocks = page.query_selector_all("div.movie.row")

            for block in movie_blocks:
                # Title
                title_el = block.query_selector("div.title-movie-list a")
                if not title_el:
                    continue
                title = title_el.inner_text().strip()
                if not title:
                    continue

                # Language: first span.label-cinema in the block
                # (may be empty string for ES; VOSE / VO as text)
                lang_el = block.query_selector("span.label-cinema")
                language = lang_el.inner_text().strip() if lang_el else "es"
                if not language:
                    language = "es"

                # Showtimes: a.sesion — skip vtadanger (those are "Ver más")
                for a_el in block.query_selector_all("a.sesion"):
                    # vtadanger = "Ver más" link, not an actual showtime
                    cls = a_el.get_attribute("class") or ""
                    if "vtadanger" in cls:
                        continue

                    time_text = a_el.inner_text().strip()
                    href = a_el.get_attribute("href") or ""
                    if not time_text or ":" not in time_text:
                        continue

                    results.append({
                        "title":    title,
                        "language": language,
                        "date":     today,
                        "time":     time_text,
                        "url":      href if href.startswith("http") else BASE_URL + href,
                    })

        except Exception as e:
            print(f"  ⚠ Lys error: {e}")

        browser.close()

    return results
