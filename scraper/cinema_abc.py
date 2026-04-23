"""
Cines ABC Valencia scraper.
Three cinemas, same platform, same DOM:

  ABC Park      — https://park.cinesabc.com/
  ABC El Saler  — https://elsaler.cinesabc.com/
  ABC Gran Turia— https://granturia.cinesabc.com/

DOM structure (server-rendered, sessions loaded async but present after
networkidle):

  div.cartelera.bloque33             → one movie card
    div.cartelera-titulo b div.ver-ficha  → title text
    div.cont-ses                     → one session
      div.hora-ses                   → first text node = "HH:MM"
        div.etiqueta-hora
          div.etiq-hora              → "(VOSE)" or "" (ES)
          div.etiq-sala              → "4K" / "PREMIUM" / …

Language: etiq-hora text contains "(VOSE)" → vose, else es.
Date:     cartelera page shows TODAY's sessions only.
Booking:  sessions have no direct URL; use cinema homepage.
"""

from datetime import date
from playwright.sync_api import sync_playwright

# (cinema_id, base_url)
CINEMAS = [
    ("abc_park",      "https://park.cinesabc.com"),
    ("abc_elsaler",   "https://elsaler.cinesabc.com"),
    ("abc_granturia", "https://granturia.cinesabc.com"),
]

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _scrape_one(page, base_url: str) -> list[dict]:
    """Scrape one ABC cinema page that is already loaded in `page`."""
    results = []
    today = date.today().isoformat()

    for block in page.query_selector_all("div.cartelera.bloque33"):
        title_el = block.query_selector("div.cartelera-titulo b div.ver-ficha")
        if not title_el:
            continue
        title = title_el.inner_text().strip()
        if not title:
            continue

        for ses in block.query_selector_all("div.cont-ses"):
            hora_el = ses.query_selector("div.hora-ses")
            if not hora_el:
                continue

            # The time is the first text node of .hora-ses;
            # inner_text() would include child text so we use JS.
            time_text = page.evaluate(
                "el => (el.childNodes[0]?.textContent || '').trim()",
                hora_el,
            )
            if not time_text or ":" not in time_text:
                continue

            # Language label lives inside .etiq-hora
            etiq_el = ses.query_selector("div.etiq-hora")
            lang_raw = etiq_el.inner_text().strip() if etiq_el else ""
            language = "vose" if "vose" in lang_raw.lower() else "es"

            results.append({
                "title":    title,
                "language": language,
                "date":     today,
                "time":     time_text,
                "url":      f"{base_url}/index?pag=cartelera",
            })

    return results


def _launch_and_scrape(base_url: str) -> list[dict]:
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page(user_agent=_UA)
        try:
            pg.goto(f"{base_url}/index?pag=cartelera", timeout=30000)
            pg.wait_for_load_state("networkidle", timeout=15000)
            results = _scrape_one(pg, base_url)
        except Exception as e:
            print(f"  ⚠ ABC ({base_url}) error: {e}")
        finally:
            browser.close()
    return results


# ── Public entry points (one per cinema, registered in run.py) ──────────────

def scrape_park() -> list[dict]:
    return _launch_and_scrape("https://park.cinesabc.com")

def scrape_elsaler() -> list[dict]:
    return _launch_and_scrape("https://elsaler.cinesabc.com")

def scrape_granturia() -> list[dict]:
    return _launch_and_scrape("https://granturia.cinesabc.com")
