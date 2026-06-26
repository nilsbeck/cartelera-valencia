"""
Yelmo Cines Valencia scraper — Mercado de Campanar location only.

Two-phase approach:

Phase 1 (cartelera listing):
  Fetch https://www.yelmocines.es/cartelera/valencia/mercado-de-campanar once
  to enumerate currently-playing movies and their /sinopsis/<slug> URLs.

Phase 2 (per-movie sinopsis page):
  For each movie, fetch /sinopsis/<slug>. The page has a <select id="ddlDate">
  date picker; changing its value re-renders the session list. For each option
  whose date is within today..today+6, select it, wait for the sessions to
  hydrate, and extract every
    <div class="now__format" data-cinema="mercado-de-campanar">
      <label>2D ESPAÑOL | (VOSE) | V.O. | …</label>
      <time class="btn"><a href="…">HH:MM</a></time>
    </div>
  emitting one showtime per session.

The cartelera page only renders a session preview without per-version <label>s,
which is why an earlier scraper got 100% ES — see commit history for context.
"""

import re
from datetime import date, datetime, timedelta
from playwright.sync_api import sync_playwright

BASE_URL  = "https://www.yelmocines.es"
CITY_SLUG = "valencia/mercado-de-campanar"

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_SPANISH_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

# .NET DateTime ticks: 1 tick = 100 ns, epoch = 0001-01-01 00:00:00 UTC.
# Yelmo's #ddlDate options use this format, e.g. 639144000000000000 = 2026-05-15.
_DOTNET_EPOCH = datetime(1, 1, 1)


def _parse_date_option(text: str) -> "str | None":
    """Extract YYYY-MM-DD from a #ddlDate option's value or visible text.

    Accepts ISO ('2026-05-15'), DD/MM/YYYY, DD-MM-YYYY, .NET tick counts, and
    Spanish dates ('Jueves, 15 de Mayo' / '15 de mayo de 2026').
    """
    if not text:
        return None
    s = text.strip()

    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"

    # Pure-digit value with magnitude consistent with .NET ticks for a date
    # in this century (≈ 6e17). Anything shorter is a year/index/etc.
    if s.isdigit() and len(s) >= 18:
        try:
            dt = _DOTNET_EPOCH + timedelta(microseconds=int(s) // 10)
            return dt.date().isoformat()
        except (OverflowError, ValueError):
            pass

    m = re.search(r"(\d{1,2})\s+(?:de\s+)?([A-Za-zñÑáéíóúÁÉÍÓÚ]+)(?:\s+de\s+(\d{4}))?", s, re.I)
    if m:
        day = int(m.group(1))
        month = _SPANISH_MONTHS.get(m.group(2).lower())
        year = int(m.group(3)) if m.group(3) else date.today().year
        if month:
            target = date(year, month, day)
            # Roll forward if the un-yeared label would otherwise be in the
            # past (e.g. December → January wrap).
            if not m.group(3) and target < date.today() - timedelta(days=1):
                target = date(year + 1, month, day)
            return target.isoformat()

    return None


_SESSION_EVAL = r"""() => {
    const out = [];

    function detectLang(row) {
        for (const label of row.querySelectorAll('label')) {
            const text = (label.textContent || '').trim().toLowerCase();
            if (!text) continue;
            if (text.includes('vose') || text.includes('v.o.s.e')) return 'VOSE';
            if (text.includes('valencià') || text.includes('valenciano')) return 'VAL';
            if (text.includes('original') || text.includes('v.o.')) return 'VO';
            if (text.includes('español') || text.includes('castellano') || text.includes('doblad')) return 'ES';
            if (/\bvo\b/.test(text)) return 'VO';
        }
        return '';
    }

    // Substring match on data-cinema in case the actual slug includes a
    // city prefix or trailing tag — 'campanar' is unique to this location.
    for (const row of document.querySelectorAll('div[data-cinema*="campanar" i]')) {
        for (const time of row.querySelectorAll('time')) {
            const timeText = (time.getAttribute('datetime') || time.textContent || '').trim();
            if (!timeText || !timeText.includes(':')) continue;
            const lang = detectLang(row);
            const a = row.querySelector('a[href]');
            out.push({ time: timeText, lang, href: a ? a.href : '' });
        }
    }
    return out;
}"""


def _log(msg: str) -> None:
    print(msg, flush=True)


def scrape() -> list[dict]:
    results = []
    today = date.today()
    target_dates = {(today + timedelta(days=i)).isoformat() for i in range(7)}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=_UA)

        # ── Phase 1: enumerate movies from the cartelera ──────────────
        # Each movie heading is an <h3> wrapping an <a href="/sinopsis/...">.
        status = None
        try:
            resp = page.goto(f"{BASE_URL}/cartelera/{CITY_SLUG}", timeout=30000)
            status = resp.status if resp else None
            page.wait_for_selector("h3 a[href*='/sinopsis/']", timeout=15000)
        except Exception as e:
            browser.close()
            raise RuntimeError(f"Yelmo cartelera load failed (status={status}): {e}") from e

        movies = page.evaluate(r"""() => {
            const seen = new Map();
            for (const a of document.querySelectorAll('h3 a[href*="/sinopsis/"]')) {
                // Preserve any query string — Yelmo may use it to carry the
                // cinema scope (city/location) into the sinopsis page.
                const href = a.href.split('#')[0];
                if (seen.has(href)) continue;
                const title = (a.textContent || '').trim();
                if (title) seen.set(href, { title, url: href });
            }
            return Array.from(seen.values());
        }""")

        if not movies:
            browser.close()
            raise RuntimeError(f"Yelmo: no movies found on cartelera (status={status})")

        # ── Phase 2: walk each movie's sinopsis page across dates ─────
        diagnosed = False
        for movie in movies:
            try:
                page.goto(movie["url"], timeout=30000)
                # state="attached": <option> children inside an unopened
                # <select> are never CSS-visible, so the default visibility
                # wait would always time out even though the DOM is ready.
                page.wait_for_selector(
                    "#ddlDate option, [data-cinema]",
                    state="attached",
                    timeout=15000,
                )
            except Exception as e:
                _log(f"  ⚠ Yelmo sinopsis fetch failed for '{movie['title']}': {e}")
                continue

            date_options = page.evaluate(r"""() => {
                const sel = document.querySelector('#ddlDate');
                if (!sel) return [];
                return Array.from(sel.options).map(o => ({
                    value: (o.value || '').trim(),
                    text:  (o.textContent || '').trim(),
                }));
            }""")

            if not diagnosed:
                cinemas = page.evaluate(r"""() => {
                    const s = new Set();
                    for (const d of document.querySelectorAll('[data-cinema]')) {
                        s.add(d.getAttribute('data-cinema'));
                    }
                    return Array.from(s);
                }""")
                _log(f"  [yelmo] data-cinema values seen: {cinemas}")
                diagnosed = True

            for opt in date_options:
                iso = _parse_date_option(opt["value"]) or _parse_date_option(opt["text"])
                if not iso or iso not in target_dates:
                    continue

                try:
                    page.select_option("#ddlDate", value=opt["value"])
                    # Some handlers listen for native 'change' rather than the
                    # synthetic event select_option emits; force-dispatch it.
                    page.evaluate(r"""() => {
                        const sel = document.querySelector('#ddlDate');
                        if (sel) sel.dispatchEvent(new Event('change', {bubbles: true}));
                    }""")
                    try:
                        page.wait_for_function(
                            r"""() => {
                                const rows = document.querySelectorAll('div[data-cinema*="campanar" i]');
                                return Array.from(rows).some(r => r.querySelectorAll('time').length > 0);
                            }""",
                            timeout=5000,
                        )
                    except Exception:
                        # Genuinely no sessions for this cinema on this day.
                        pass
                except Exception as e:
                    _log(f"  ⚠ Yelmo select_option failed ({movie['title']} {iso}): {e}")
                    continue

                sessions = page.evaluate(_SESSION_EVAL)
                for s in sessions:
                    time_text = (s.get("time") or "")[:5]
                    if not time_text or ":" not in time_text:
                        continue
                    href = s.get("href") or movie["url"]
                    results.append({
                        "title":    movie["title"],
                        "language": s.get("lang") or "es",
                        "date":     iso,
                        "time":     time_text,
                        "url":      href if href.startswith("http") else BASE_URL + href,
                    })

        browser.close()

    return results
