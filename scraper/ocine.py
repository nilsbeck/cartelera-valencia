"""
Ocine Premium Aqua Valencia scraper.
URL: https://www.ocinepremiumaqua.es/

Two-phase approach:

Phase 1 (Playwright, main page):
  Discover currently-showing films with their detail-page URLs.
  Each film block (div.peli-item.element-item) contains a link to
  /film-{id}/p?{slug}= which is the per-film session page.

Phase 2 (Playwright, per-film date pages):
  For each film URL × date (today → next Thursday):
    Navigate to {film_url}&selectedDate={YYYY-MM-DD}
    Parse sessions from table.planificacions → tr.plans rows.
    Language is detected from tr class names and adjacent version labels.

Spanish cinemas programme on a Friday→Thursday weekly cycle, so sessions
are published from today through the upcoming Thursday.

Film page URL pattern:
  https://www.ocinepremiumaqua.es/film-{id}/p?{movie-slug}=&selectedDate=YYYY-MM-DD
"""

import re
from datetime import date, timedelta
from playwright.sync_api import sync_playwright

BASE_URL = "https://www.ocinepremiumaqua.es"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

_FILM_LINK_RE = re.compile(r"/film-\d+/p")

# Ordered so longer/more-specific tokens are checked first.
# "v.o.s.e" and "subtitulad" must precede "v.o." and "original"
# so VOSE beats VO when both substrings are present.
_LANG_TOKENS: list[tuple[str, str]] = [
    ("vose",       "vose"),
    ("v.o.s.e",    "vose"),
    ("subtitulad", "vose"),
    ("v.o.",       "vo"),
    ("original",   "vo"),
    ("doblad",     "es"),
    ("castellano", "es"),
    ("español",    "es"),
]


def _dates_until_next_thursday() -> list[str]:
    """
    Return ISO date strings from today through the upcoming Thursday (inclusive).
    Covers the current Spanish cinema week (Friday → Thursday cycle).
    If today is Thursday, returns today through the following Thursday (8 days).
    """
    today = date.today()
    days_ahead = (3 - today.weekday()) % 7  # 3 = Thursday
    if days_ahead == 0:
        days_ahead = 7
    end = today + timedelta(days=days_ahead)
    result = []
    d = today
    while d <= end:
        result.append(d.isoformat())
        d += timedelta(days=1)
    return result


def _detect_lang(text: str) -> str:
    """Infer a raw language tag from a version-label string."""
    t = text.lower()
    for token, lang in _LANG_TOKENS:
        if token in t:
            return lang
    return "es"


def scrape() -> list[dict]:
    results = []
    dates = _dates_until_next_thursday()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=_UA)

        # ── Phase 1: discover film detail URLs ───────────────────────────
        film_entries: list[dict] = []
        try:
            # wait_until="commit" fires as soon as response headers arrive,
            # then we wait separately for the content selector. This avoids
            # the 60 s domcontentloaded timeout on sites that delay that event.
            page.goto(BASE_URL, timeout=60000, wait_until="commit")
            page.wait_for_selector("div.peli-item.element-item", timeout=90000)

            for block in page.query_selector_all("div.peli-item.element-item"):
                h4 = block.query_selector("h4")
                if not h4:
                    continue
                title = h4.inner_text().strip()
                if not title:
                    continue

                # Find the link to /film-{id}/p within this block
                link_el = (
                    block.query_selector("a[href*='/film-'][href*='/p']")
                    or h4.query_selector("a[href*='/film-']")
                )
                if not link_el:
                    continue
                href = link_el.get_attribute("href") or ""
                if not _FILM_LINK_RE.search(href):
                    continue

                film_url = href if href.startswith("http") else BASE_URL + href
                # Drop any selectedDate already in the link
                film_url = re.sub(r"[&?]selectedDate=[^&]*", "", film_url).rstrip("?&")
                film_entries.append({"title": title, "url": film_url})

        except Exception as e:
            print(f"  ⚠ Ocine (discovery) error: {e}")

        # ── Phase 2: per-film, per-date session scraping ─────────────────
        for entry in film_entries:
            title    = entry["title"]
            film_url = entry["url"]
            sep      = "&" if "?" in film_url else "?"

            for date_str in dates:
                target = f"{film_url}{sep}selectedDate={date_str}"
                try:
                    page.goto(target, timeout=30000, wait_until="domcontentloaded")
                except Exception:
                    continue

                for tr in page.query_selector_all("tr.plans"):
                    cls = tr.get_attribute("class") or ""

                    # Language priority:
                    # 1. Token in the tr's own class string
                    # 2. A version-label element inside the tr
                    # 3. Text of the immediately preceding sibling tr (date/header row)
                    lang = _detect_lang(cls)

                    if lang == "es":
                        label_el = tr.query_selector(
                            "td.versio, td.version, td.idioma, "
                            "span.versio, span.version, span.idioma, "
                            "div.versio, div.version, .horasessio-titol, "
                            ".film-version, .session-version"
                        )
                        if label_el:
                            lang = _detect_lang(label_el.inner_text())

                    if lang == "es":
                        prev_text = tr.evaluate(
                            "el => { "
                            "  const p = el.previousElementSibling; "
                            "  return p ? p.innerText : ''; "
                            "}"
                        )
                        if prev_text:
                            lang = _detect_lang(prev_text)

                    for btn in tr.query_selector_all("div.horasessio button"):
                        time_text = btn.inner_text().strip()
                        if not time_text or ":" not in time_text:
                            continue
                        results.append({
                            "title":    title,
                            "language": lang,
                            "date":     date_str,
                            "time":     time_text,
                            "url":      film_url,
                        })

        browser.close()

    return results
