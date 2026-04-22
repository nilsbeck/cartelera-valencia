"""
Cine Lys Valencia scraper.
Small independent cinema — likely simpler static HTML.
Check: https://www.cinelys.es  (verify URL)
"""

from datetime import date, timedelta
from playwright.sync_api import sync_playwright

BASE_URL = "https://www.cinelys.es"


def scrape() -> list[dict]:
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ))

        try:
            # Lys may show a weekly schedule on one page
            page.goto(f"{BASE_URL}/cartelera", timeout=15000)
            page.wait_for_load_state("networkidle", timeout=10000)

            for offset in range(7):
                target   = date.today() + timedelta(days=offset)
                date_str = target.isoformat()
                # Adapt date-based filtering to Lys's actual structure
                day_label = target.strftime("%-d").lower()  # e.g. "21"

                movie_blocks = page.query_selector_all(
                    ".pelicula, .movie, article, .film-block"
                )
                for block in movie_blocks:
                    title_el = block.query_selector("h2, h3, .title, .nombre")
                    if not title_el:
                        continue
                    title = title_el.inner_text().strip()

                    lang_el  = block.query_selector(".idioma, .vo, .version")
                    language = lang_el.inner_text().strip() if lang_el else "VO"

                    for t_el in block.query_selector_all("a.hora, a.sesion, .horario a, .tiempo"):
                        time_text = t_el.inner_text().strip()
                        href      = t_el.get_attribute("href") or BASE_URL
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
            print(f"  ⚠ Lys error: {e}")

        browser.close()

    return results
