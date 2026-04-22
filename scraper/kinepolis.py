"""
Kinépolis Valencia scraper.
Kinépolis has a well-structured site with per-cinema pages.

Valencia cinema slug: check https://kinepolis.es/cines/kinepolis-valencia
"""

from datetime import date, timedelta
from playwright.sync_api import sync_playwright

BASE_URL    = "https://kinepolis.es"
CINEMA_SLUG = "kinepolis-valencia"   # adjust to real URL slug


def scrape() -> list[dict]:
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
            url = f"{BASE_URL}/cines/{CINEMA_SLUG}/cartelera?date={date_str}"

            try:
                page.goto(url, timeout=20000)
                page.wait_for_load_state("networkidle", timeout=12000)

                # Kinépolis typically renders movie cards with class .movie or similar
                # Inspect and adapt selectors below
                movie_blocks = page.query_selector_all(
                    ".movie-container, .film-item, article.movie"
                )

                for block in movie_blocks:
                    title_el = block.query_selector("h2, h3, .movie-title, .title")
                    if not title_el:
                        continue
                    title = title_el.inner_text().strip()

                    # Kinépolis often shows language inline with session button text
                    # e.g. "20:30 VO" or groups by tab
                    session_els = block.query_selector_all(
                        "a.session-link, button.session, .showtime-btn"
                    )

                    for s_el in session_els:
                        full_text = s_el.inner_text().strip()
                        href      = s_el.get_attribute("href") or "#"

                        # Try to extract time and language from text like "20:30 VO"
                        parts    = full_text.split()
                        time_str = parts[0] if parts else ""
                        language = parts[1] if len(parts) > 1 else "ES"

                        if ":" not in time_str:
                            continue

                        results.append({
                            "title":    title,
                            "language": language,
                            "date":     date_str,
                            "time":     time_str,
                            "url":      href if href.startswith("http") else BASE_URL + href,
                        })

            except Exception as e:
                print(f"  ⚠ Kinépolis error on {date_str}: {e}")
                continue

        browser.close()

    return results
