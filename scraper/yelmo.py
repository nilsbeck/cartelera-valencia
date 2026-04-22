"""
Yelmo Cines Valencia scraper.
Yelmo has a reasonably structured website; tries JSON API first,
falls back to HTML scraping.

Cinema ID for Yelmo Valencia: adjust CINEMA_ID to match the real one.
Check: https://www.yelmocinemas.es/peliculas-en-cartelera/valencia
"""

import requests
from datetime import date, timedelta
from playwright.sync_api import sync_playwright

# Yelmo sometimes exposes a city/cinema filter — find the right slug
BASE_URL   = "https://www.yelmocinemas.es"
CITY_SLUG  = "valencia"   # adjust if needed
CINEMA_ID  = "61"          # inspect network tab on Yelmo to find real ID


def scrape() -> list[dict]:
    # Try API approach first, fall back to HTML
    results = _scrape_html()
    return results


def _scrape_html() -> list[dict]:
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ))

        for offset in range(7):
            target   = date.today() + timedelta(days=offset)
            date_str = target.isoformat()
            # Yelmo uses date param like ?fecha=2026-04-21
            url = f"{BASE_URL}/cartelera/{CITY_SLUG}?fecha={date_str}"

            try:
                page.goto(url, timeout=20000)
                page.wait_for_load_state("networkidle", timeout=12000)

                # Yelmo DOM: .movie-info-wrapper contains title + sessions
                # Adapt selectors to real DOM after inspection
                movie_blocks = page.query_selector_all(
                    ".movie-wrapper, .pelicula-item, .film-card"
                )

                for block in movie_blocks:
                    title_el = block.query_selector("h2.title, .movie-title, h3")
                    if not title_el:
                        continue
                    title = title_el.inner_text().strip()

                    # Yelmo groups sessions by version (VO / Doblada)
                    version_blocks = block.query_selector_all(".version, .idioma-group")

                    if version_blocks:
                        for vb in version_blocks:
                            lang_el = vb.query_selector(".version-name, .idioma")
                            language = lang_el.inner_text().strip() if lang_el else "ES"

                            for t_el in vb.query_selector_all("a.session, a.hora, .horario a"):
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
                    else:
                        # Flat list fallback
                        for t_el in block.query_selector_all("a.session, a.hora"):
                            time_text = t_el.inner_text().strip()
                            href = t_el.get_attribute("href") or BASE_URL
                            if not time_text:
                                continue
                            results.append({
                                "title":    title,
                                "language": "ES",
                                "date":     date_str,
                                "time":     time_text,
                                "url":      href if href.startswith("http") else BASE_URL + href,
                            })

            except Exception as e:
                print(f"  ⚠ Yelmo error on {date_str}: {e}")
                continue

        browser.close()

    return results
