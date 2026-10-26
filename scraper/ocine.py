"""
Ocine Premium Aqua Valencia scraper.

Around October 2026 ocinepremiumaqua.es was rebuilt on a site builder and the
film tiles (div.peli-item, /film-{id}/p links) this scraper read are gone; the
homepage now only embeds banners. Sessions live in the ticketing app at
tickets.ocinepremiumaqua.es (SystemBCN), which reads a JSON API. We call that
API directly — shapes captured from live traffic with ocine_api_probe.py:

  POST /api/v1/init      {idioma, urlWeb, …}          → {token, …}
  POST /api/v1/sessions  {token, idioma, urlWeb}      → {pelicules, anticipades,
                                                         estrenes, …}
       film list only; each film's `sessions` is empty here.
  GET  /api/v1/pelicula/{id}?lang=es                  → the film with sessions:
       esGrup false → sessions[] on the film itself
       esGrup true  → subPelicules[] (one per version), each with
                      propietats[] and sessions[{data, hora, nomSala, …}]
       catalegPropietats[] maps property codes to labels; the version ones
       seen live are 13 VO, 14 VOSE, 15 CATALÀ, 16 EUSKERA. No version code
       means the dubbed Spanish version.

The origin drops connections from cloud IPs (PR #57), so this runs on the home
server, and through Playwright: the calls are made with fetch() from a page on
tickets.ocinepremiumaqua.es itself, so they are same-origin and look like the
app's own.

Spanish cinemas programme on a Friday→Thursday cycle, so sessions cover
today through the upcoming Thursday.
"""

import json
from datetime import date, timedelta

from playwright.sync_api import sync_playwright

TICKETS = "https://tickets.ocinepremiumaqua.es"
APP_URL = f"{TICKETS}/?nocap=1#/"
FILM_URL = f"{TICKETS}/?nocap=1#/DetallPelicula/{{id}}"

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0 Safari/537.36"
)

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

# Version property codes as seen in catalegPropietats on 2026-10-26, used when
# a response does not carry the catalogue. CATALÀ is the Valencian version.
_VERSION_CODES = {13: "vo", 14: "vose", 15: "val", 16: "eus"}
_VERSION_NAMES = {"VO": "vo", "VOSE": "vose", "CAT": "val", "EUS": "eus"}

_GET_JS = """async (url) => {
    const r = await fetch(url, {headers: {accept: 'application/json'}});
    return {status: r.status, text: await r.text()};
}"""
_POST_JS = """async ({url, body}) => {
    const r = await fetch(url, {method: 'POST', headers: {'content-type': 'application/json'}, body});
    return {status: r.status, text: await r.text()};
}"""


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


def _version_lang(props: list, catalogue: list, title: str = "") -> str:
    """Language tag for a film or sub-film from its property codes.

    Codes are resolved through the response's own catalegPropietats (by its
    short name) when present, falling back to the codes seen live. A title
    like "Clayface (VOSE)" is checked last, for a version listed without its
    property. Euskera is reported as-is; run.py maps unknown tags to ES.
    """
    names = {c.get("codi"): (c.get("nomCurt") or "").upper() for c in catalogue or []}
    for code in props or []:
        if names.get(code) in _VERSION_NAMES:
            return _VERSION_NAMES[names[code]]
        if code not in names and code in _VERSION_CODES:
            return _VERSION_CODES[code]
    return _detect_lang(title) if title else "es"


def _extract_sessions(film: dict, title: str, target_dates: set) -> list[dict]:
    """Showtime rows from a /api/v1/pelicula/{id} answer."""
    catalogue = film.get("catalegPropietats") or []
    versions = film.get("subPelicules") if film.get("esGrup") else None
    if not versions:
        versions = [film]
    url = FILM_URL.format(id=film.get("id"))
    rows, seen = [], set()
    for version in versions:
        lang = _version_lang(version.get("propietats"), catalogue, version.get("titol") or "")
        for s in version.get("sessions") or []:
            day = (s.get("data") or "")[:10]
            hhmm = (s.get("hora") or "")[:5]
            if day not in target_dates or len(hhmm) != 5 or ":" not in hhmm:
                continue
            key = (day, hhmm, lang)
            if key in seen:
                continue
            seen.add(key)
            rows.append({"title": title, "language": lang, "date": day, "time": hhmm, "url": url})
    return rows


def _films_to_fetch(listing: dict, target_dates: set) -> "dict[int, str]":
    """Film ids → titles from a /api/v1/sessions answer.

    Everything in `pelicules` (now showing) and `anticipades` (presales), plus
    `estrenes` (coming soon) whose release date falls inside the window.
    """
    films: dict[int, str] = {}
    for key in ("pelicules", "anticipades", "estrenes"):
        for f in listing.get(key) or []:
            if not isinstance(f, dict) or f.get("id") is None or not f.get("titol"):
                continue
            if key == "estrenes" and (f.get("dataEstrena") or "")[:10] not in target_dates:
                continue
            films.setdefault(f["id"], f["titol"].strip())
    return films


class _Api:
    def __init__(self, page):
        self.page = page

    def _parse(self, what: str, resp: dict) -> dict:
        if resp["status"] != 200:
            raise RuntimeError(f"Ocine {what}: HTTP {resp['status']} {resp['text'][:200]}")
        try:
            return json.loads(resp["text"])
        except ValueError as e:
            raise RuntimeError(f"Ocine {what}: not JSON: {resp['text'][:200]}") from e

    def post(self, path: str, body: dict) -> dict:
        return self._parse(path, self.page.evaluate(_POST_JS, {"url": TICKETS + path, "body": json.dumps(body)}))

    def get(self, path: str) -> dict:
        return self._parse(path, self.page.evaluate(_GET_JS, TICKETS + path))


def scrape() -> "tuple[list[dict], list[str]]":
    dates = _dates_until_next_thursday()
    target_dates = set(dates)
    results: list[dict] = []
    diags: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=_UA, locale="es-ES")
            try:
                page.goto(APP_URL, timeout=45000)
            except Exception as e:
                raise RuntimeError(f"Ocine ticketing app load failed: {e}") from e
            api = _Api(page)

            init = api.post("/api/v1/init", {
                "idioma": "es", "plan": None, "butaques": None, "nume": None,
                "urlconfirmation": None, "urlinici": None, "deluxe": None, "app": None,
                "cli": None, "urlWeb": APP_URL, "dataSessio": None, "horaSessio": None,
                "extern": None, "nomesbar": None,
            })
            token = init.get("token")
            if not token:
                raise RuntimeError(f"Ocine init returned no token (error={init.get('error')!r})")

            listing = api.post("/api/v1/sessions", {
                "token": token, "idioma": "es", "urlWeb": f"{TICKETS}/?nocap=1#/SeleccioPelicula",
            })
            films = _films_to_fetch(listing, target_dates)
            if not films:
                raise RuntimeError(f"Ocine: film list empty (keys: {sorted(listing)})")
            _log(f"  [ocine] {len(films)} films listed")

            errors: list[str] = []
            for film_id, title in films.items():
                try:
                    film = api.get(f"/api/v1/pelicula/{film_id}?lang=es")
                except Exception as e:
                    errors.append(f"{title}: {e}")
                    continue
                results.extend(_extract_sessions(film, title, target_dates))

            per_day = {d: sum(1 for r in results if r["date"] == d) for d in dates}
            _log(f"  [ocine] showtimes per day: {per_day}")
            diags.append(f"showtimes per day: {per_day}")
            if errors:
                _log(f"  ⚠ Ocine: {len(errors)} film requests failed, first: {errors[0]}")
                diags.append(f"{len(errors)} film requests failed, first: {errors[0]}")
                if len(errors) == len(films):
                    raise RuntimeError(f"Ocine: every film request failed, first: {errors[0]}")
        finally:
            browser.close()

    return results, diags
