"""
Ocine Premium Aqua Valencia scraper.
URL: https://www.ocinepremiumaqua.es/

Two-phase scrape with a runtime switch between Playwright (default) and
a requests+BS4 path via the EU fetch-proxy (see ../proxy/).

Why the switch:
  - From a US GitHub-hosted runner the origin silently drops connections
    (bbfe860, PR #56, PR #57). Even Render's Frankfurt egress hits a TCP
    connect-timeout — the block is by cloud-IP, not geo.
  - From a residential IP (home server in Docker, see ../DOCKER.md) the
    origin responds, but the page is client-rendered: requests+BS4 lands
    on an empty shell. Playwright renders it correctly.

So:
  - OCINE_PROXY_URL unset (home / direct path) → Playwright two-phase.
  - OCINE_PROXY_URL set (proxy path, only useful if the proxy host can
    render JS — currently the bundled proxy can't, so this path is
    effectively disabled until someone bolts a headless browser on it).

Phase 1 (main page):
  Wait for film tiles to render, then enumerate (title, /film-{id}/p URL).

Phase 2 (per-film date pages):
  For each film × date in today..next-Thursday, navigate to
    {film_url}&selectedDate={YYYY-MM-DD}
  wait for the session table to hydrate, then parse tr.plans rows.

Language priority inside a tr.plans row:
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
from playwright.sync_api import sync_playwright

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


# ── Playwright path (used when running from a residential IP) ────────────────

def _goto_with_retry(page, url: str, label: str) -> "int | None":
    """Navigate with wait_until='commit' and one retry.

    Skips the retry on `ERR_CONNECTION_CLOSED` / `ERR_CONNECTION_REFUSED` —
    those indicate the origin is actively rejecting (rate-guard, IP-block).
    A second attempt only makes the rate-guard angrier and doubles the
    error-storm length.
    """
    last_exc: "Exception | None" = None
    for attempt in (1, 2):
        try:
            resp = page.goto(url, timeout=15000, wait_until="commit")
            return resp.status if resp else None
        except Exception as e:
            last_exc = e
            _log(f"  ⚠ Ocine {label} goto attempt {attempt} failed: {e}")
            msg = str(e)
            if "ERR_CONNECTION_CLOSED" in msg or "ERR_CONNECTION_REFUSED" in msg:
                break
    raise last_exc if last_exc else RuntimeError("goto failed without exception")


def _scrape_playwright(dates: list[str]) -> list[dict]:
    results: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=_UA)

        # ── Phase 1: enumerate film tiles ─────────────────────────────
        try:
            _goto_with_retry(page, BASE_URL, "main page")
            page.wait_for_selector(
                "div.peli-item, a[href*='/film-']",
                state="attached",
                timeout=20000,
            )
        except Exception as e:
            _log(f"  ⚠ Ocine main page failed: {e}")
            browser.close()
            return []

        films = page.evaluate(r"""() => {
            // Explicit array + Set so the returned shape is unambiguous
            // (PR #59's Map+shorthand variant produced an entry with
            // url=undefined that JSON-serialised to {"title": ...} only,
            // crashing Phase 2 with KeyError('url')).
            const out = [];
            const seenUrls = new Set();
            const re = /\/film-\d+\/p/;
            for (const block of document.querySelectorAll('div.peli-item')) {
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
                if (!url || seenUrls.has(url)) continue;
                seenUrls.add(url);
                out.push({"title": title, "url": url});
            }
            return out;
        }""")

        _log(f"  [ocine] Phase 1: {len(films)} films")

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
        # Circuit breaker — Ocine's rate-guard returns ERR_CONNECTION_CLOSED
        # after a handful of rapid requests. Once that starts, every
        # remaining navigation will fail too, so stop early instead of
        # logging 200+ lines of identical error noise. We keep whatever
        # sessions Phase 2 had already collected.
        MAX_CONSECUTIVE_FAILURES = 5
        consecutive_failures = 0
        aborted = False

        diagnosed = False
        for film in films:
            if aborted:
                break
            if not isinstance(film, dict):
                _log(f"  ⚠ Ocine: unexpected film entry type {type(film).__name__}: {film!r}")
                continue
            film_url = film.get("url")
            title    = film.get("title")
            if not film_url or not title:
                _log(f"  ⚠ Ocine: skipping malformed film entry: {film!r}")
                continue
            sep = "&" if "?" in film_url else "?"
            for date_str in dates:
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    _log(
                        f"  ⚠ Ocine: {MAX_CONSECUTIVE_FAILURES} consecutive "
                        f"failures, aborting remaining {films.__len__()} films "
                        f"(likely rate-limited by upstream)"
                    )
                    aborted = True
                    break

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
                    consecutive_failures += 1
                    _log(f"  ⚠ Ocine session fetch failed ({title} {date_str}): {e}")
                    continue

                # Success — clear the circuit-breaker counter.
                consecutive_failures = 0

                if not diagnosed:
                    try:
                        d = page.evaluate(r"""() => ({
                            url: location.href,
                            plans_rows: document.querySelectorAll('tr.plans').length,
                            buttons:    document.querySelectorAll('tr.plans button').length,
                            body_len:   document.body.innerHTML.length,
                        })""")
                        _log(f"  [ocine] DIAG first session page: {d}")
                    except Exception:
                        pass
                    diagnosed = True

                results.extend(_parse_sessions(html, title, film_url, date_str))

        browser.close()
    return results


# ── Proxy path (kept for the manual workflow_dispatch on GH Actions) ─────────

def _make_proxy_fetcher():
    """Returns fetch_html(url, *, timeout) that calls the configured proxy.

    Caller must check OCINE_PROXY_URL is non-empty before invoking.
    """
    proxy_url   = os.environ["OCINE_PROXY_URL"].rstrip("/")
    proxy_token = os.environ.get("OCINE_PROXY_TOKEN", "")
    sess = requests.Session()
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

    return fetch_html


def _scrape_via_proxy(dates: list[str]) -> list[dict]:
    """requests+BS4 path. Only useful if the proxy host can render JS;
    currently the bundled Render proxy can't, so this returns empty."""
    fetch_html = _make_proxy_fetcher()
    results: list[dict] = []

    try:
        html = fetch_html(BASE_URL, timeout=60)
    except Exception as e:
        _log(f"  ⚠ Ocine main page failed (via proxy): {e}")
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

    _log(f"  [ocine] Phase 1 (proxy): {len(films)} films")

    if not films:
        _log(
            f"  [ocine] DIAG main page (proxy): peli_item_divs="
            f"{len(soup.find_all('div', class_=lambda c: c and 'peli-item' in c))}, "
            f"film_links={len(soup.find_all('a', href=_FILM_LINK_RE))}, "
            f"body_len={len(html)}"
        )
        return []

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
                    f"  [ocine] DIAG first session page (proxy): "
                    f"plans_rows={len(soup2.find_all('tr', class_='plans'))}, "
                    f"buttons={len(soup2.select('tr.plans button'))}, "
                    f"body_len={len(html)}"
                )
                diagnosed = True
            results.extend(_parse_sessions(html, film["title"], film_url, date_str))

    return results


def scrape() -> list[dict]:
    dates = _dates_until_next_thursday()
    if os.environ.get("OCINE_PROXY_URL"):
        _log("  [ocine] fetch mode: proxy")
        return _scrape_via_proxy(dates)
    _log("  [ocine] fetch mode: playwright (direct)")
    return _scrape_playwright(dates)
