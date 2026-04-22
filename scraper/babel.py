"""
Cine Babel Valencia scraper.
URL: https://www.cinebabel.com  (adjust if needed)

Returns list of dicts:
  { title, language, date, time, url }
"""

from datetime import date, timedelta
from playwright.sync_api import sync_playwright

BASE_URL = "https://www.cinebabel.com"


def scrape() -> list[dict]:
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ))

        # Scrape today + next 6 days
        for offset in range(7):
            target = date.today() + timedelta(days=offset)
            date_str = target.isoformat()

            # ── Adjust selector logic to match Babel's actual HTML structure ──
            # This is a template; inspect the live site and update selectors.
            try:
                page.goto(f"{BASE_URL}/cartelera", timeout=15000)
                page.wait_for_load_state("networkidle", timeout=10000)

                # Example: each movie block has class .movie-item
                # Adapt these selectors to the real DOM
                movie_blocks = page.query_selector_all(".pelicula, .movie-item, article.film")

                for block in movie_blocks:
                    title_el = block.query_selector(".titulo, .title, h2, h3")
                    if not title_el:
                        continue
                    title = title_el.inner_text().strip()

                    # Language: look for VO/VOS/ES label near showtimes
                    lang_el = block.query_selector(".idioma, .language, .vo-badge")
                    language = lang_el.inner_text().strip() if lang_el else "ES"

                    # Showtime links
                    time_els = block.query_selector_all("a.sesion, a.showtime, .horario a")
                    for t_el in time_els:
                        time_text = t_el.inner_text().strip()
                        href = t_el.get_attribute("href") or BASE_URL
                        if not time_text:
                            continue
                        results.append({
                            "title":    title,
                            "language": language,
                            "date":     date_str,
                            "time":     time_text,
                            "url":      href if href.startswith("http") else BASE_URL + href,
                        })
            except Exception as e:
                print(f"  ⚠ Babel error on {date_str}: {e}")
                continue

        browser.close()

    return results
