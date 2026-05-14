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
# Tries four strategies in order:
#   1. Date cell inside the session element itself (table-row pattern)
#   2. data-date / data-fecha / data-dia attribute on session or ancestor
#   3. Nearest preceding sibling (at any ancestor level) whose text looks like a date
#   4. Falls back to empty string (triggers diagnostic print in Python)
_JS_SESSIONS = """
() => {
    const MONTH_WORD_RE = /enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre/i;
    const NUMERIC_DATE_RE = /\\b(\\d{1,2})[\\/-](\\d{1,2})(?:[\\/-](\\d{2,4}))?\\b/;
    const ISO_DATE_RE = /\\b(\\d{4})-(\\d{2})-(\\d{2})\\b/;

    function looksLikeDate(text) {
        if (!text || text.includes(':')) return false;  // skip times like "17:30"
        const t = text.toLowerCase().trim();
        if (t.length > 120) return false;
        return MONTH_WORD_RE.test(t) || NUMERIC_DATE_RE.test(t) || ISO_DATE_RE.test(t);
    }

    function dataDateOf(el) {
        return el.dataset.date || el.dataset.fecha || el.dataset.dia || el.dataset.day || '';
    }

    function findDateText(ses) {
        // Strategy 1: date inside session (e.g. first <td> in a table row)
        const inner = ses.querySelector(
            '[class*="fecha"], [class*="date"], [class*="dia"], [class*="day"], td:first-child'
        );
        if (inner) {
            const t = (inner.textContent || '').trim();
            if (looksLikeDate(t)) return t;
        }

        // Strategy 2: data attribute on session or any ancestor
        let node = ses;
        while (node && node !== document.body) {
            const d = dataDateOf(node);
            if (d) return d;
            node = node.parentElement;
        }

        // Strategy 3: nearest preceding sibling (walk up the tree)
        node = ses;
        while (node && node !== document.body) {
            let sib = node.previousElementSibling;
            while (sib) {
                const t = (sib.textContent || '').trim();
                if (looksLikeDate(t)) return t;
                sib = sib.previousElementSibling;
            }
            node = node.parentElement;
        }

        return '';
    }

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
        return {
            time,
            lang:     etiq ? etiq.textContent.trim() : '',
            dateText: findDateText(ses),
        };
    }).filter(Boolean);
}
"""

# Diagnostic JS: dumps context around the first session element.
# Called only when sessions are found but all have empty dateText.
_JS_DIAGNOSTIC = """
() => {
    const ses = document.querySelector('div.cont-ses, .cont-ses, div.hora-ses');
    if (!ses) return {found: false, url: location.href};

    // Parent chain (up to 6 levels)
    const chain = [];
    let node = ses;
    while (node && node !== document.body && chain.length < 6) {
        const ownText = Array.from(node.childNodes)
            .filter(n => n.nodeType === 3)
            .map(n => n.textContent.trim())
            .filter(t => t)
            .join(' | ');
        chain.push({tag: node.tagName, cls: node.className, id: node.id,
                    ownText, data: JSON.stringify(node.dataset)});
        node = node.parentElement;
    }

    // Previous siblings of the session and its parent
    function prevSibsOf(el, limit) {
        const out = [];
        let sib = el ? el.previousElementSibling : null;
        while (sib && out.length < limit) {
            out.push({tag: sib.tagName, cls: sib.className, id: sib.id,
                      text: (sib.textContent || '').trim().slice(0, 200),
                      data: JSON.stringify(sib.dataset)});
            sib = sib.previousElementSibling;
        }
        return out;
    }

    return {
        found:         true,
        url:           location.href,
        chain,
        sesOwnSibs:    prevSibsOf(ses, 5),
        parentSibs:    prevSibsOf(ses.parentElement, 5),
        sesHtml:       ses.outerHTML.slice(0, 800),
    };
}
"""


def _parse_ficha_date(text: str) -> "str | None":
    """
    Parse a date string into ISO format.
    Handles: 'Jueves, 14 de mayo', '14/05', '14-05-2026', '2026-05-14'.
    Returns None if unparseable.
    """
    t = text.strip()
    today = date.today()

    # ISO format YYYY-MM-DD (from data-date attributes)
    m_iso = re.match(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m_iso:
        try:
            return date(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))).isoformat()
        except (ValueError, TypeError):
            pass

    tl = t.lower()

    # Word-month format: "14 de mayo", "14 mayo", "jueves, 14 de mayo", etc.
    m = re.search(r"(\d{1,2})\s+(?:de\s+)?([a-záéíóúñ]+)", tl)
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

    # Numeric format "DD/MM" or "DD-MM" or "DD/MM/YYYY"
    # Use a word boundary to avoid matching "2026-05-14" as day=20, month=26
    m2 = re.search(r"(?<!\d)(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?(?!\d)", tl)
    if m2:
        try:
            day, month = int(m2.group(1)), int(m2.group(2))
            if 1 <= day <= 31 and 1 <= month <= 12:
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

    # Diagnostic: if all sessions have empty dateText, dump DOM context once per ficha
    if all(not s.get("dateText") for s in raw_sessions):
        try:
            diag = page.evaluate(_JS_DIAGNOSTIC)
            print(f"  ⚠ ABC empty dateText for '{title}' — diagnostic: {diag}")
        except Exception as _de:
            print(f"  ⚠ ABC diagnostic error for '{title}': {_de}")

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
