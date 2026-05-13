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
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
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
    const MONTHS = {
        'enero':1,'febrero':2,'marzo':3,'abril':4,'mayo':5,'junio':6,
        'julio':7,'agosto':8,'septiembre':9,'octubre':10,'noviembre':11,'diciembre':12
    };
    function looksLikeDate(text) {
        return /\\d{1,2}\\s+(?:de\\s+)?[a-záéíóúñ]{4,}/.test(text.toLowerCase());
    }
    function findDateText(el) {
        let node = el;
        while (node && node !== document.body) {
            let sib = node.previousElementSibling;
            while (sib) {
                const t = (sib.textContent || '').trim();
                if (t.length < 80 && looksLikeDate(t)) return t;
                sib = sib.previousElementSibling;
            }
            node = node.parentElement;
        }
        return '';
    }
    return Array.from(document.querySelectorAll('div.cont-ses')).map(ses => {
        const horaEl = ses.querySelector('div.hora-ses');
        if (!horaEl) return null;
        const time = (horaEl.childNodes[0] ? horaEl.childNodes[0].textContent : '').trim();
        if (!time.includes(':')) return null;
        const etiq = ses.querySelector('div.etiq-hora');
        return {
            time:     time,
            lang:     etiq ? etiq.textContent.trim() : '',
            dateText: findDateText(ses)
        };
    }).filter(Boolean);
}
"""


def _parse_ficha_date(text: str) -> "str | None":
    """
    Parse a Spanish date string like 'Jueves, 14 de mayo' → '2026-05-14'.
    Returns None if unparseable.
    """
    m = re.search(r"(\d{1,2})\s+(?:de\s+)?([a-záéíóúñ]+)", text.lower())
    if not m:
        return None
    try:
        day   = int(m.group(1))
        month = _MONTHS.get(m.group(2))
        if not month:
            return None
        today  = date.today()
        target = date(today.year, month, day)
        if target < today - timedelta(days=1):
            target = date(today.year + 1, month, day)
        return target.isoformat()
    except (ValueError, TypeError):
        return None


def _detect_language(etiq_el) -> str:
    """Return raw language code from the .etiq-hora element."""
    if etiq_el is None:
        return "es"
    text = etiq_el.inner_text().strip().lower()
    if "vose" in text:
        return "vose"
    if text in ("vo", "v.o.", "v.o.s.", "v.o.s.e."):
        return "vo"
    return "es"


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
            page.goto(f"{base_url}/index?pag={pag}", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)
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

    return entries


def _scrape_ficha(page, ficha_url: str, title: str) -> list[dict]:
    """
    Visit a movie ficha page and return all upcoming sessions with dates.
    Uses JS DOM traversal to pair sessions with their nearest date header.
    """
    try:
        page.goto(ficha_url, timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception as e:
        print(f"  ⚠ ABC ficha error ({ficha_url}): {e}")
        return []

    try:
        raw_sessions = page.evaluate(_JS_SESSIONS)
    except Exception:
        return []

    today = date.today()
    results = []
    for item in raw_sessions:
        date_str = _parse_ficha_date(item.get("dateText", ""))
        if not date_str:
            continue
        if date.fromisoformat(date_str) < today:
            continue

        lang_text = (item.get("lang") or "").lower()
        if "vose" in lang_text:
            lang = "vose"
        elif lang_text in ("vo", "v.o.", "v.o.s.", "v.o.s.e."):
            lang = "vo"
        else:
            lang = "es"

        results.append({
            "title":    title,
            "language": lang,
            "date":     date_str,
            "time":     item["time"],
            "url":      ficha_url,
        })

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
