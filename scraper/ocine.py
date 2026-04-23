"""
Ocine Premium Aqua Valencia scraper.
URL: https://www.ocinepremiumaqua.es/

DOM structure (Joomla + custom JS cinema module — client-rendered):
  div#filmContainer
    div.peli-item.element-item   [id="{MovieTitle}"]  → one movie
      h4                                               → title
      div.horarisContainer
        table.planificacions
          tr.{YYYY-MM-DD}           → date header row
          tr.{YYYY-MM-DD}.plans     → session times row (hidden by default for
                                      non-today dates, but present in DOM)
            div.horasessio button   → "HH:MM"

Language: no visible indicator on this multiplex → default "es".
Date: ISO date extracted from class name of tr.plans rows.
Booking URL: constructed from peli-item[id] (lowercased), which the JS passes
  as the URLinici query parameter when a session button is clicked:
  https://tickets.ocinepremiumaqua.es/compra/show_numerada_confirmation.php
    ?URLinici=https%3A%2F%2Fwww.ocinepremiumaqua.es%2F%3F{slug}
"""

import re
from datetime import date, timedelta
from urllib.parse import quote
from playwright.sync_api import sync_playwright

BASE_URL     = "https://www.ocinepremiumaqua.es"
TICKETS_BASE = "https://tickets.ocinepremiumaqua.es/compra/show_numerada_confirmation.php"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def scrape() -> list[dict]:
    results = []
    today = date.today()
    valid_dates = {(today + timedelta(days=i)).isoformat() for i in range(7)}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page(user_agent=_UA)
        try:
            pg.goto(BASE_URL, timeout=60000, wait_until="domcontentloaded")
            pg.wait_for_selector("div.peli-item.element-item", timeout=20000)

            for block in pg.query_selector_all("div.peli-item.element-item"):
                # Title
                h4 = block.query_selector("h4")
                if not h4:
                    continue
                title = h4.inner_text().strip()
                if not title:
                    continue

                # Booking URL: peli-item[id] lowercased is the movie slug
                # used in the tickets subdomain URLinici parameter
                movie_slug = (block.get_attribute("id") or "").lower()
                if movie_slug:
                    urilinici = quote(f"{BASE_URL}/?{movie_slug}", safe="")
                    film_url = f"{TICKETS_BASE}?URLinici={urilinici}"
                else:
                    film_url = BASE_URL

                # Each tr.plans row has the ISO date in its class
                for tr in block.query_selector_all("tr.plans"):
                    cls = tr.get_attribute("class") or ""
                    m = _ISO_DATE_RE.search(cls)
                    if not m:
                        continue
                    date_str = m.group(1)
                    if date_str not in valid_dates:
                        continue

                    for btn in tr.query_selector_all("div.horasessio button"):
                        time_text = btn.inner_text().strip()
                        if not time_text or ":" not in time_text:
                            continue
                        results.append({
                            "title":    title,
                            "language": "es",
                            "date":     date_str,
                            "time":     time_text,
                            "url":      film_url,
                        })

        except Exception as e:
            print(f"  ⚠ Ocine error: {e}")
        finally:
            browser.close()

    return results
