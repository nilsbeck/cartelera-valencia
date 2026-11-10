"""
Cines ABC Valencia scraper.

  ABC Park       — https://www.cinesabc.com/cine/abcpark       (recinto 2)
  ABC El Saler   — https://www.cinesabc.com/cine/abcelsaler    (recinto 172)
  ABC Gran Turia — https://www.cinesabc.com/cine/abcgranturia  (recinto 171)

In November 2026 Cines ABC replaced the old per-cinema PHP sites
(park.cinesabc.com etc., which now only JS-redirect to cinesabc.com) with a
single Flutter web app. It paints to a <canvas>, so there is no DOM left to
scrape. Instead we drive the real app in Playwright and read the JSON it
fetches for itself from the ticketing backend (reservaentradas.com):

Phase 1 — cinema page  (/cine/<sub>)
  The app requests  <sub>.reservaentradas.com/<sub>/index?recinto=N…
  → {"Eventos": [...on screen...], "ProximosEventos": [...upcoming...]}
  Each event carries only today's sessions, so we only use it for the list.

Phase 2 — movie page  (/e/<sub>/<slug>/<recinto>/<eventId>)
  The app requests  <sub>.reservaentradas.com/<sub>/sesiones?…&evento=ID…
  → {"Evento": {"Titulo", "Sesiones": [{"FechaSesion": "DD/MM/YYYY",
                                         "Horatxt": "HH:MM",
                                         "Formato": "(VOSE)" | "", ...}]}}
  i.e. every upcoming session of that movie, all days.

Language comes from the per-session "Formato" label (same "(VOSE)" text the
old site showed in .etiq-hora); an empty label means dubbed (ES).
"""

import asyncio
import re
from datetime import date, timedelta

from playwright.async_api import async_playwright

SITE = "https://www.cinesabc.com"

# (cinema_id, subdominio, recinto id)
CINEMAS = [
    ("abc_park",      "abcpark",      2),
    ("abc_elsaler",   "abcelsaler",   172),
    ("abc_granturia", "abcgranturia", 171),
]

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

# Movie pages loaded at the same time (each one boots the Flutter app).
_CONCURRENCY = 4
_INDEX_TIMEOUT_MS = 60000
_SESIONES_TIMEOUT_MS = 30000

_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}


def _parse_ficha_date(text: str) -> "str | None":
    """
    Parse a date string into ISO format.
    Handles: '10/11/2026' (API FechaSesion), 'Jueves, 14 de mayo', '14/05',
    '14-05-2026', '2026-05-14'.
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
    """Return raw language code from a DOM element's label text."""
    if etiq_el is None:
        return "es"
    return _detect_lang_from_text(etiq_el.inner_text())


# ── Pure helpers for the JSON payloads (unit-tested) ────────────────────────

def _session_lang(sesion: dict) -> str:
    """Raw language code for one session of the sesiones/index JSON."""
    label = (sesion.get("Formato") or "").strip().strip("()").strip()
    return _detect_lang_from_text(label)


def _session_time(sesion: dict) -> "str | None":
    raw = (sesion.get("Horatxt") or sesion.get("Hora") or "").strip()
    m = re.match(r"(\d{1,2}):(\d{2})", raw)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def _events_to_visit(index: dict) -> dict[int, str]:
    """{event_id: title} for every movie on screen or upcoming."""
    events: dict[int, str] = {}
    for key in ("Eventos", "ProximosEventos"):
        for ev in index.get(key) or []:
            try:
                ev_id = int(ev["Id"])
            except (KeyError, TypeError, ValueError):
                continue
            title = (ev.get("Titulo") or "").strip()
            if title and ev_id not in events:
                events[ev_id] = title
    return events


def _movie_url(sub: str, recinto: int, ev_id: int, title: str = "") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "pelicula"
    return f"{SITE}/e/{sub}/{slug}/{recinto}/{ev_id}"


def _extract_sessions(evento: dict, url: str, today: "date | None" = None) -> list[dict]:
    """Turn one Evento (with its Sesiones) into raw showtime rows."""
    today = today or date.today()
    title = (evento.get("Titulo") or "").strip()
    if not title:
        return []
    rows, seen = [], set()
    for ses in evento.get("Sesiones") or []:
        date_str = _parse_ficha_date(ses.get("FechaSesion") or ses.get("Fecha") or "")
        time_str = _session_time(ses)
        if not date_str or not time_str:
            continue
        if date.fromisoformat(date_str) < today:
            continue
        lang = _session_lang(ses)
        key = (date_str, time_str, lang)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "title":    title,
            "language": lang,
            "date":     date_str,
            "time":     time_str,
            "url":      url,
        })
    return rows


# ── Browser driving ─────────────────────────────────────────────────────────

async def _fetch_index(context, sub: str) -> dict:
    page = await context.new_page()
    try:
        async with page.expect_response(
            lambda r: f"/{sub}/index?" in r.url and r.status == 200,
            timeout=_INDEX_TIMEOUT_MS,
        ) as info:
            await page.goto(f"{SITE}/cine/{sub}", wait_until="commit")
        resp = await info.value
        return await resp.json()
    finally:
        await page.close()


async def _fetch_event(context, sem, sub: str, recinto: int,
                       ev_id: int, title: str) -> "dict | None":
    url = _movie_url(sub, recinto, ev_id, title)
    async with sem:
        page = await context.new_page()
        try:
            async with page.expect_response(
                lambda r: f"/{sub}/sesiones?" in r.url
                          and f"evento={ev_id}" in r.url and r.status == 200,
                timeout=_SESIONES_TIMEOUT_MS,
            ) as info:
                await page.goto(url, wait_until="commit")
            resp = await info.value
            data = await resp.json()
            evento = data.get("Evento") if isinstance(data, dict) else None
            if isinstance(evento, list):
                evento = evento[0] if evento else None
            return evento if isinstance(evento, dict) else None
        except Exception as e:
            print(f"  ⚠ ABC ({sub}) sessions for '{title}' ({ev_id}) failed: {e}")
            return None
        finally:
            await page.close()


async def _scrape_cinema(sub: str, recinto: int) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=_UA)
        try:
            index = await _fetch_index(context, sub)
            events = _events_to_visit(index)
            if not events:
                print(f"  ⚠ ABC ({sub}) cartelera loaded but listed no movies")
                return []

            sem = asyncio.Semaphore(_CONCURRENCY)
            ids = list(events)
            fetched = await asyncio.gather(*(
                _fetch_event(context, sem, sub, recinto, i, events[i]) for i in ids
            ))

            # Fallback: if a movie page failed, keep at least today's
            # sessions from the listing payload.
            from_index = {
                int(ev["Id"]): ev
                for key in ("Eventos", "ProximosEventos")
                for ev in index.get(key) or [] if "Id" in ev
            }
            results = []
            for ev_id, evento in zip(ids, fetched):
                evento = evento or from_index.get(ev_id) or {}
                url = (evento.get("url_de_compra")
                       or _movie_url(sub, recinto, ev_id, events[ev_id]))
                results.extend(_extract_sessions(evento, url))
            return results
        finally:
            await browser.close()


def _launch_and_scrape(sub: str, recinto: int) -> list[dict]:
    # run.py calls scrapers from worker threads, which have no event loop.
    return asyncio.run(_scrape_cinema(sub, recinto))


# ── Public entry points (one per cinema, registered in run.py) ──────────────

def scrape_park() -> list[dict]:
    return _launch_and_scrape("abcpark", 2)

def scrape_elsaler() -> list[dict]:
    return _launch_and_scrape("abcelsaler", 172)

def scrape_granturia() -> list[dict]:
    return _launch_and_scrape("abcgranturia", 171)
