"""
Ocine Premium Aqua Valencia scraper.
URL: https://www.ocinepremiumaqua.es/

Two-phase Playwright approach. The main page is a JS-rendered Isotope grid
(div.peli-item.element-item entries are arranged client-side) and the per-film
schedule table is hydrated after the date selection round-trips, so plain
requests+BeautifulSoup gets an empty shell — that's what disabled this
scraper in commit bbfe860 ("connectivity is resolved" never happened because
the issue was rendering, not connectivity).

Phase 1 (main page):
  Wait for film tiles to render, then enumerate (title, /film-{id}/p URL).

Phase 2 (per-film date pages):
  For each film × date in today..next-Thursday, navigate to
    {film_url}&selectedDate={YYYY-MM-DD}
  wait for the session table to hydrate, then parse tr.plans rows.
  Language priority:
    1. version token in the tr's own class string
    2. version-label element inside the row
    3. preceding sibling row's text

Spanish cinemas programme on a Friday→Thursday cycle, so sessions cover
today through the upcoming Thursday.
"""

import re
from datetime import date, timedelta
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE_URL = "https://ocinepremiumaqua.es"

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0 Safari/537.36"
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


def _log(msg: str) -> None:
    print(msg, flush=True)


def _parse_sessions(html: str, title: str, film_url: str, date_str: str) -> list[dict]:
    """Parse the session table from a film-detail page's rendered HTML."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for tr in soup.find_all("tr", class_="plans"):
        cls = " ".join(tr.get("class") or [])

        lang = _detect_lang(cls)

        if lang == "es":
            label_el = tr.find(class_=lambda c: c and any(
                x in " ".join(c) for x in
                ["versio", "version", "idioma", "horasessio-titol",
                 "film-version", "session-version"]
            ))
            if label_el:
                lang = _detect_lang(label_el.get_text())

        if lang == "es":
            prev = tr.find_previous_sibling("tr")
            if prev:
                lang = _detect_lang(prev.get_text())

        for btn in tr.find_all("button"):
            time_text = btn.get_text(strip=True)
            if not time_text or ":" not in time_text:
                continue
            out.append({
                "title":    title,
                "language": lang,
                "date":     date_str,
                "time":     time_text,
                "url":      film_url,
            })
    return out


def _goto_with_retry(page, url: str, label: str) -> "int | None":
    """Navigate with wait_until='commit' and one retry.

    'commit' fires as soon as the HTTP response starts arriving, instead of
    waiting for DOMContentLoaded — Ocine's homepage holds DOMContentLoaded
    open past 30s while late-loading third-party scripts (analytics, etc.)
    settle, even though the markup we actually need is already in the DOM.
    Subsequent wait_for_selector calls are the real readiness signal.
    """
    last_exc: "Exception | None" = None
    for attempt in (1, 2):
        try:
            resp = page.goto(url, timeout=60000, wait_until="commit")
            return resp.status if resp else None
        except Exception as e:
            last_exc = e
            _log(f"  ⚠ Ocine {label} goto attempt {attempt} failed: {e}")
    raise last_exc if last_exc else RuntimeError("goto failed without exception")


def scrape() -> list[dict]:
    results: list[dict] = []
    dates = _dates_until_next_thursday()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=_UA)

        # ── Phase 1: enumerate film tiles ─────────────────────────────
        status = None
        try:
            status = _goto_with_retry(page, BASE_URL, "main page")
            page.wait_for_selector(
                "div.peli-item, a[href*='/film-']",
                state="attached",
                timeout=20000,
            )
        except Exception as e:
            _log(f"  ⚠ Ocine main page failed (status={status}): {e}")
            browser.close()
            return []

        films = page.evaluate(r"""() => {
            const seen = new Map();
            const blocks = document.querySelectorAll('div.peli-item');
            const re = /\/film-\d+\/p/;
            for (const block of blocks) {
                const h4 = block.querySelector('h4');
                if (!h4) continue;
                const title = (h4.textContent || '').trim();
                if (!title) continue;
                const a = block.querySelector('a[href*="/film-"]');
                if (!a) continue;
                const href = a.getAttribute('href') || '';
                if (!re.test(href)) continue;
                let url = href.startsWith('http') ? href : (location.origin + href);
                url = url.replace(/[?&]selectedDate=[^&]*/g, '').replace(/[?&]$/, '');
                if (!seen.has(url)) seen.set(url, { title, url });
            }
            return Array.from(seen.values());
        }""")

        _log(f"  [ocine] Phase 1: {len(films)} films (HTTP {status})")

        if not films:
            try:
                diag = page.evaluate(r"""() => ({
                    url: location.href,
                    peli_items: document.querySelectorAll('div.peli-item').length,
                    any_film_links: document.querySelectorAll('a[href*="/film-"]').length,
                    h4_count: document.querySelectorAll('h4').length,
                    body_len: document.body.innerHTML.length,
                })""")
                _log(f"  [ocine] DIAG main page: {diag}")
            except Exception:
                pass
            browser.close()
            return []

        # ── Phase 2: per-film, per-date session pages ─────────────────
        diagnosed = False
        for film in films:
            film_url = film["url"]
            sep = "&" if "?" in film_url else "?"
            for date_str in dates:
                target = f"{film_url}{sep}selectedDate={date_str}"
                try:
                    _goto_with_retry(page, target, f"session {date_str}")
                    try:
                        page.wait_for_selector(
                            "tr.plans, .no-sesiones, .sin-sesiones",
                            state="attached",
                            timeout=8000,
                        )
                    except Exception:
                        pass
                    html = page.content()
                except Exception as e:
                    _log(f"  ⚠ Ocine session fetch failed ({film['title']} {date_str}): {e}")
                    continue

                if not diagnosed:
                    try:
                        d = page.evaluate(r"""() => ({
                            url: location.href,
                            plans_rows: document.querySelectorAll('tr.plans').length,
                            buttons:    document.querySelectorAll('tr.plans button').length,
                            body_len:   document.body.innerHTML.length,
                        })""")
                        _log(f"  [ocine] DIAG session page: {d}")
                    except Exception:
                        pass
                    diagnosed = True

                results.extend(_parse_sessions(html, film["title"], film_url, date_str))

        browser.close()

    return results
