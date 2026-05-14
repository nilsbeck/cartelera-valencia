"""
Cines ABC Valencia scraper.
Three cinemas, same platform, same DOM:

  ABC Park      — https://park.cinesabc.com/
  ABC El Saler  — https://elsaler.cinesabc.com/
  ABC Gran Turia— https://granturia.cinesabc.com/

Two-phase approach:

Phase 1 (Playwright, listing pages):
  Collect movie ficha URLs from BOTH the main cartelera AND the VOSE page.
  Visiting only ?pag=cartelera misses movies that appear exclusively on
  the VOSE programme (?pag=vose).

  DOM structure (same on both listing pages):
    div.cartelera.bloque33
      a[href*="pag=ficha"]                    → ficha URL (pag=ficha&evento=N)
      div.cartelera-titulo b div.ver-ficha    → title

Phase 2 (Playwright, per-movie ficha pages):
  Each ficha page shows all upcoming sessions for one movie grouped by date.
  We use a JS walk to pair every div.cont-ses with its nearest preceding
  Spanish date header (e.g. "Jueves, 14 de mayo"), then extract time and
  language from each session.

  Session DOM (same as listing pages):
    div.cont-ses
      div.hora-ses          → first text node = "HH:MM"
        div.etiqueta-hora
          div.etiq-hora     → "(VOSE)" / "VO" / "" (ES)
"""

import re
from datetime import date, timedelta
from playwright.sync_api import sync_playwright

CINEMAS = [
    ("abc_park",      "https://park.cinesabc.com"),
    ("abc_elsaler",   "https://elsaler.cinesabc.com"),
    ("abc_granturia", "https://granturia.cinesabc.com"),
]

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}

# JavaScript that returns [{time, lang, dateText}] for all sessions on the page.
# For each div.cont-ses it walks backwards up the DOM to find the nearest
# element whose text looks like a Spanish date ("14 de mayo", "jueves 14 mayo").
_JS_SESSIONS = """
() => {
    const MONTH_RE = /enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre/i;

    // Collect every element whose direct text looks like a Spanish date heading.
    // "Direct text" = text nodes that are immediate children (not deep descendants),
    // so container divs that happen to contain month words are excluded.
    function collectDateEls() {
        const out = [];
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
        let el;
        while ((el = walker.nextNode())) {
            const ownText = Array.from(el.childNodes)
                .filter(n => n.nodeType === Node.TEXT_NODE)
                .map(n => n.textContent.trim())
                .filter(t => t.length > 0)
                .join(' ');
            if (ownText.length > 2 && ownText.length < 80
                    && MONTH_RE.test(ownText) && /\\d{1,2}/.test(ownText)) {
                out.push({el, text: ownText});
            }
        }
        // Fallback: consider any short leaf element whose full text looks like a date
        if (out.length === 0) {
            const walker2 = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
            while ((el = walker2.nextNode())) {
                if (el.children.length > 0) continue;  // leaf elements only
                const t = (el.textContent || '').trim();
                if (t.length > 2 && t.length < 80 && MONTH_RE.test(t) && /\\d{1,2}/.test(t)) {
                    out.push({el, text: t});
                }
            }
        }
        return out;
    }

    const dateEls = collectDateEls();

    const SESS_SELS = ['div.cont-ses', '.cont-ses', 'div.sesion-item', 'div.session-item', 'li.sesion'];
    let sessions = [];
    for (const sel of SESS_SELS) {
        sessions = Array.from(document.querySelectorAll(sel));
        if (sessions.length > 0) break;
    }

    return sessions.map(ses => {
        const horaEl = ses.querySelector('div.hora-ses, .hora-ses, .hora, .time');
        if (!horaEl) return null;
        const time = (horaEl.childNodes[0] ? horaEl.childNodes[0].textContent : '').trim();
        if (!time.includes(':')) return null;
        const etiq = ses.querySelector('div.etiq-hora, .etiq-hora, .etiqueta, .version, .lang');

        // Find the date element that most recently precedes this session in document order.
        // compareDocumentPosition returns a bitmask; bit 4 (value 4) means the argument
        // (ses) follows the context object (dateEl) — i.e. dateEl comes before ses.
        let dateText = '';
        for (const {el: dateEl, text} of dateEls) {
            if (dateEl.compareDocumentPosition(ses) & 4) {
                dateText = text;
            }
        }

        return {
            time,
            lang:     etiq ? etiq.textContent.trim() : '',
            dateText,
        };
    }).filter(Boolean);
}
"""


def _parse_ficha_date(text: str) -> "str | None":
    """
    Parse a Spanish date string like 'Jueves, 14 de mayo' → '2026-05-14'.
    Also handles numeric formats like '14/05' or '14-05-2026'.
    Returns None if unparseable.
    """
    t = text.lower()
    today = date.today()

    # Try word-month format first: "14 de mayo", "14 mayo", etc.
    m = re.search(r"(\d{1,2})\s+(?:de\s+)?([a-záéíóúñ]+)", t)
    if m:
        try:
            day   = int(m.group(1))
            month = _MONTHS.get(m.group(2))
            if month:
                target = date(today.year, month, day)
                if target < today - timedelta(days=1):
                    target = date(today.year + 1, month, day)
                return target.isoformat()
        except (ValueError, TypeError):
            pass

    # Fallback: numeric format "DD/MM" or "DD-MM" or "DD/MM/YYYY"
    m2 = re.search(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?", t)
    if m2:
        try:
            day, month = int(m2.group(1)), int(m2.group(2))
            year_raw = m2.group(3)
            if year_raw:
                year = int(year_raw) if len(year_raw) == 4 else 2000 + int(year_raw)
            else:
                year = today.year
                target = date(year, month, day)
                if target < today - timedelta(days=1):
                    year += 1
            return date(year, month, day).isoformat()
        except (ValueError, TypeError):
            pass

    return None


def _detect_lang_from_text(text: str) -> str:
    """Return raw language code from a label string (case-insensitive)."""
    t = text.strip().lower()
    if "vose" in t:
        return "vose"
    if t in ("vo", "v.o.", "v.o.s.", "v.o.s.e."):
        return "vo"
    return "es"


def _detect_language(etiq_el) -> str:
    """Return raw language code from the .etiq-hora DOM element."""
    if etiq_el is None:
        return "es"
    return _detect_lang_from_text(etiq_el.inner_text())


def _collect_ficha_urls(page, base_url: str) -> dict[str, str]:
    """
    Visit both listing pages and return {ficha_url: title} for all movies found.
    Visiting both ?pag=cartelera and ?pag=vose ensures VOSE-exclusive titles
    are included alongside the main programme.
    """
    entries: dict[str, str] = {}
    today = date.today().isoformat()

    for pag in ("cartelera", "vose"):
        try:
            page.goto(f"{base_url}/index?pag={pag}", timeout=30000, wait_until="domcontentloaded")
            try:
                page.wait_for_selector("div.cartelera.bloque33, a[href*='pag=ficha']", timeout=15000)
            except Exception:
                pass  # content may already be present
        except Exception as e:
            print(f"  ⚠ ABC ({base_url} ?pag={pag}) load error: {e}")
            continue

        for block in page.query_selector_all("div.cartelera.bloque33"):
            title_el = block.query_selector("div.cartelera-titulo b div.ver-ficha")
            if not title_el:
                continue
            title = title_el.inner_text().strip()
            if not title:
                continue

            ficha_el = block.query_selector("a[href*='pag=ficha']")
            if not ficha_el:
                continue
            href = ficha_el.get_attribute("href") or ""
            ficha_url = href if href.startswith("http") else f"{base_url}/{href.lstrip('/')}"

            if ficha_url not in entries:
                entries[ficha_url] = title

    if not entries:
        print(f"  ⚠ ABC ({base_url}) page loaded but found no movies")
    return entries


def _scrape_ficha(page, ficha_url: str, title: str) -> list[dict]:
    """
    Visit a movie ficha page and return all upcoming sessions with dates.
    Uses JS DOM traversal to pair sessions with their nearest date header.
    """
    try:
        page.goto(ficha_url, timeout=30000, wait_until="domcontentloaded")
        try:
            page.wait_for_selector("div.cont-ses, div.hora-ses", timeout=10000)
        except Exception:
            pass  # selector may not exist on this page
    except Exception as e:
        print(f"  ⚠ ABC ficha error ({ficha_url}): {e}")
        return []

    try:
        raw_sessions = page.evaluate(_JS_SESSIONS)
    except Exception as e:
        print(f"  ⚠ ABC JS eval error for '{title}': {e}")
        return []

    if not raw_sessions:
        print(f"  ⚠ ABC no sessions found for: {title} ({ficha_url})")
        return []

    today = date.today()
    results = []
    unparseable_dates = []
    for item in raw_sessions:
        date_str = _parse_ficha_date(item.get("dateText", ""))
        if not date_str:
            unparseable_dates.append(repr(item.get("dateText", "")))
            continue
        if date.fromisoformat(date_str) < today:
            continue

        lang = _detect_lang_from_text(item.get("lang") or "")

        results.append({
            "title":    title,
            "language": lang,
            "date":     date_str,
            "time":     item["time"],
            "url":      ficha_url,
        })

    if not results and unparseable_dates:
        sample = unparseable_dates[:3]
        print(f"  ⚠ ABC date parse failed for '{title}' — dateText samples: {sample}")

    return results


def _launch_and_scrape(base_url: str) -> list[dict]:
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=_UA)
        try:
            # Phase 1: collect ficha URLs from both listing pages
            ficha_map = _collect_ficha_urls(page, base_url)

            # Phase 2: scrape each ficha page for multi-day sessions
            for ficha_url, title in ficha_map.items():
                results.extend(_scrape_ficha(page, ficha_url, title))

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
