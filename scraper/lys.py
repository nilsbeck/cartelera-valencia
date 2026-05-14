"""
Cines Lys Valencia scraper.
URL: https://www.reservaentradas.com/cine/valencia/cineslys

Two-phase approach:

Phase 1 (Playwright, cinema page):
  For each movie block collect title, language, and the sesiones page URL.

Phase 2 (Playwright, per-movie sesiones page):
  Navigate to each /sesiones/ page (JS-rendered) and parse date tabs + session
  links.  We reuse the same browser page to avoid launching a new browser.

  Date tabs: <li><a href="#N">Day DD/MM</a></li>
  Session sections: <div id="N"> containing <a href="/entrada/...">HH:MM</a>
"""

import re
from datetime import date, timedelta
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE_URL    = "https://www.reservaentradas.com"
CINEMA_PATH = "/cine/valencia/cineslys"

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0 Safari/537.36"
)


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


def _fetch_session_map(page, sesiones_url: str) -> "dict[tuple[str,str], str]":
    """
    Navigate to a /sesiones/ page with Playwright and return
    {(date_str, time_str): booking_url}.  Returns empty dict on failure.
    """
    try:
        page.goto(sesiones_url, timeout=20000, wait_until="domcontentloaded")
        try:
            page.wait_for_selector("a[href^='#']", timeout=10000)
        except Exception:
            pass
        html = page.content()
    except Exception:
        return {}

    soup = BeautifulSoup(html, "html.parser")
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
    today = date.today().isoformat()
    phase1: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=_UA)

        try:
            page.goto(f"{BASE_URL}{CINEMA_PATH}", timeout=30000, wait_until="domcontentloaded")
            try:
                page.wait_for_selector("div.movie.row", timeout=15000)
            except Exception:
                pass

            for block in page.query_selector_all("div.movie.row"):
                title_el = block.query_selector("div.title-movie-list a")
                if not title_el:
                    continue
                title = title_el.inner_text().strip()
                if not title:
                    continue

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

                lang_el = block.query_selector("span.label-cinema")
                language = lang_el.inner_text().strip() if lang_el else "es"
                if not language:
                    language = "es"

                direct = []
                for a_el in block.query_selector_all("a.sesion"):
                    cls = a_el.get_attribute("class") or ""
                    if "vtadanger" in cls:
                        continue
                    time_text = a_el.inner_text().strip()
                    a_href = a_el.get_attribute("href") or ""
                    if not time_text or ":" not in time_text:
                        continue
                    direct.append({
                        "date": today,
                        "time": time_text,
                        "url":  a_href if a_href.startswith("http") else BASE_URL + a_href,
                    })

                phase1.append({
                    "title":        title,
                    "language":     language,
                    "sesiones_url": sesiones_url,
                    "direct":       direct,
                })

        except Exception as e:
            print(f"  ⚠ Lys error: {e}")
        else:
            if not phase1:
                print("  ⚠ Lys page loaded but found no movie blocks")

        # Phase 2: fetch per-movie sesiones pages for multi-day sessions
        # Reuse the same browser page so JS rendering works correctly.
        session_cache: dict[str, dict[tuple[str, str], str]] = {}
        for entry in phase1:
            url = entry["sesiones_url"]
            if url and url not in session_cache:
                session_cache[url] = _fetch_session_map(page, url)

        browser.close()

    for entry in phase1:
        session_map = session_cache.get(entry["sesiones_url"], {})
        if session_map:
            for (date_str, time_str), url in session_map.items():
                results.append({
                    "title":    entry["title"],
                    "language": entry["language"],
                    "date":     date_str,
                    "time":     time_str,
                    "url":      url,
                })
        elif entry["direct"]:
            print(f"  ⚠ Lys Phase 2 empty for '{entry['title']}', using today's sessions")
            for d in entry["direct"]:
                results.append({
                    "title":    entry["title"],
                    "language": entry["language"],
                    **d,
                })

    return results
