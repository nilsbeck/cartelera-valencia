"""
Ocine Premium Aqua Valencia scraper.
URL: https://www.ocinepremiumaqua.es/

The site is server-rendered HTML (commit 1b91936 confirmed requests+BS4 was
sufficient when reachable). The blocker isn't rendering, it's network: from
GitHub-hosted runner IPs the origin silently drops connections (no HTTP
response ever arrives, see commit bbfe860). We route through a tiny EU
fetch-proxy on Render (see ../proxy/) when OCINE_PROXY_URL is configured,
falling back to direct fetches otherwise so the scraper degrades cleanly
in local development.

Phase 1 (main page):
  Enumerate film tiles (div.peli-item.element-item → h4 title + /film-{id}/p
  link).

Phase 2 (per-film date pages):
  For each film × date in today..next-Thursday, GET
    {film_url}&selectedDate={YYYY-MM-DD}
  and parse table.planificacions → tr.plans rows. Language priority:
    1. version token in the tr's own class string
    2. version-label element inside the row
    3. preceding sibling row's text

Spanish cinemas programme on a Friday→Thursday cycle, so sessions cover
today through the upcoming Thursday.
"""

import os
import re
from datetime import date, timedelta
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://ocinepremiumaqua.es"

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0 Safari/537.36"
)

_DIRECT_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.google.es/",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

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
    """ISO date strings from today through the upcoming Thursday (inclusive)."""
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


def _make_fetcher():
    """Return (fetch_html, mode_label).

    fetch_html(url, *, timeout) -> str. Routes through the configured EU
    proxy when OCINE_PROXY_URL is set; otherwise hits the origin directly.
    """
    proxy_url   = os.environ.get("OCINE_PROXY_URL",   "").rstrip("/")
    proxy_token = os.environ.get("OCINE_PROXY_TOKEN", "")
    sess = requests.Session()

    if proxy_url:
        proxy_headers = {"X-Proxy-Token": proxy_token} if proxy_token else {}
        proxy_headers["X-Forward-User-Agent"] = _UA

        def fetch_html(url: str, timeout: int = 30) -> str:
            r = sess.get(
                f"{proxy_url}/fetch",
                params={"url": url},
                headers=proxy_headers,
                timeout=timeout,
            )
            r.raise_for_status()
            return r.text

        return fetch_html, "proxy"

    sess.headers.update(_DIRECT_HEADERS)

    def fetch_html(url: str, timeout: int = 30) -> str:
        r = sess.get(url, timeout=timeout)
        r.raise_for_status()
        return r.text

    return fetch_html, "direct"


def _parse_sessions(html: str, title: str, film_url: str, date_str: str) -> list[dict]:
    """Parse the session table from a film-detail page's HTML."""
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


def scrape() -> list[dict]:
    results: list[dict] = []
    dates = _dates_until_next_thursday()
    fetch_html, mode = _make_fetcher()
    _log(f"  [ocine] fetch mode: {mode}")

    # ── Phase 1: enumerate film tiles ─────────────────────────────────
    # First call may include a Render free-tier cold-start (~10–30s).
    try:
        html = fetch_html(BASE_URL, timeout=60)
    except Exception as e:
        _log(f"  ⚠ Ocine main page failed: {e}")
        return []

    soup = BeautifulSoup(html, "html.parser")
    films: list[dict] = []
    seen_urls: set[str] = set()

    for block in soup.find_all("div", class_=lambda c: c and "peli-item" in c):
        h4 = block.find("h4")
        if not h4:
            continue
        title = h4.get_text(strip=True)
        if not title:
            continue

        link = block.find("a", href=_FILM_LINK_RE) or h4.find("a", href=_FILM_LINK_RE)
        if not link:
            continue
        href = link.get("href", "")
        if not _FILM_LINK_RE.search(href):
            continue

        film_url = href if href.startswith("http") else BASE_URL + href
        film_url = re.sub(r"[&?]selectedDate=[^&]*", "", film_url).rstrip("?&")
        if film_url in seen_urls:
            continue
        seen_urls.add(film_url)
        films.append({"title": title, "url": film_url})

    _log(f"  [ocine] Phase 1: {len(films)} films")

    if not films:
        _log(
            f"  [ocine] DIAG main page: peli_item_divs="
            f"{len(soup.find_all('div', class_=lambda c: c and 'peli-item' in c))}, "
            f"film_links={len(soup.find_all('a', href=_FILM_LINK_RE))}, "
            f"body_len={len(html)}"
        )
        return []

    # ── Phase 2: per-film, per-date session pages ─────────────────────
    diagnosed = False
    for film in films:
        film_url = film["url"]
        sep = "&" if "?" in film_url else "?"
        for date_str in dates:
            target = f"{film_url}{sep}selectedDate={date_str}"
            try:
                html = fetch_html(target, timeout=30)
            except Exception as e:
                _log(f"  ⚠ Ocine session fetch failed ({film['title']} {date_str}): {e}")
                continue

            if not diagnosed:
                soup2 = BeautifulSoup(html, "html.parser")
                _log(
                    f"  [ocine] DIAG first session page: "
                    f"plans_rows={len(soup2.find_all('tr', class_='plans'))}, "
                    f"buttons={len(soup2.select('tr.plans button'))}, "
                    f"body_len={len(html)}"
                )
                diagnosed = True

            results.extend(_parse_sessions(html, film["title"], film_url, date_str))

    return results
