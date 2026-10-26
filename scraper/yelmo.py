"""
Cine Yelmo Valencia scraper — Mercado de Campanar location only.

Since 2026-10-24 yelmocines.es redirects to cineyelmo.com, a Next.js app that
renders nothing server-side and gets all its data from Cinépolis's GraphQL
API at api-g.cinepolis.com. We query that API directly instead of the DOM.

Both hosts sit behind a Cloudflare WAF that 403s datacenter IPs and non-browser
clients, so requests go through Playwright: load the homepage once (which
clears Cloudflare and makes the site send its own billboard request), copy the
headers of that request, and replay them from the same browser context so our
calls look exactly like the site's.

Phase 1 — movies at the cinema (operation `Movies`, captured from live traffic):
  POST /v2/billboards/graphql
  {countryId: "ES", category: "now-playing" | "pre-sale",
   cinemas: "mercado-de-campanar-valencia", limit: 100}

Phase 2 — sessions per movie (operation `BillboardOrderLinking`, taken from the
site's JS; it uses the same Apollo client as `Movies`, hence the same URL):
  {countryId, movieId, cinemaId, controlTime: 0, isAbsoluteExperiencePath}
  → schedules[].dates[].languages[{language, displayLanguage}].showtimes[]
If that operation is refused, the older `Billboard` operation (same response
shape, `cinemas` + `timezone` variables) is tried instead.

Cinema ids come from the `Cities` operation on
/shared-services/locations/graphql: Mercado de Campanar is
"mercado-de-campanar-valencia" (Vista id 811).
"""

import json
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

SITE_URL     = "https://cineyelmo.com/es"
BILLBOARDS   = "https://api-g.cinepolis.com/v2/billboards/graphql"
COUNTRY_ID   = "ES"
CINEMA_ID    = "mercado-de-campanar-valencia"
MADRID       = ZoneInfo("Europe/Madrid")

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_MOVIES_QUERY = """
query Movies($countryId: String!, $category: String, $cinemas: String, $after: String, $limit: Int, $isAbsoluteMediaPath: Boolean) {
  movies(countryId: $countryId, category: $category, cinemas: $cinemas, after: $after, limit: $limit, isAbsoluteMediaPath: $isAbsoluteMediaPath) {
    totalCount
    edges { node { id name originalName categories } }
  }
}"""

_SHOWTIME_FIELDS = """
      dates
      schedules {
        movieId
        dates {
          date
          languages {
            language
            displayLanguage
            showtimes { sessionId datetime screen format { name } }
          }
        }
      }"""

_ORDER_LINKING_QUERY = """
query BillboardOrderLinking($countryId: String!, $movieId: String!, $cinemaId: String!, $controlTime: Int!, $isAbsoluteExperiencePath: Boolean!) {
  billboardOrderLinking(countryId: $countryId, movieId: $movieId, cinemaId: $cinemaId, controlTime: $controlTime, isAbsoluteExperiencePath: $isAbsoluteExperiencePath) {%s
  }
}""" % _SHOWTIME_FIELDS

_BILLBOARD_QUERY = """
query Billboard($countryId: String!, $movieId: String!, $cinemas: String!, $timezone: String) {
  billboard(countryId: $countryId, movieId: $movieId, cinemas: $cinemas, timezone: $timezone) {%s
  }
}""" % _SHOWTIME_FIELDS

# Headers the browser computes per request; replaying them would be wrong.
_DROP_HEADERS = {"content-length", "host", "cookie", "accept-encoding", "connection"}


def _log(msg: str) -> None:
    print(msg, flush=True)


def _detect_language(language: str, display: str) -> str:
    """Map the API's (language, displayLanguage) pair to VOSE / VAL / VO / ES.

    The exact codes the API uses for Spain were not in the captured traffic,
    so this matches on substrings of both fields. English or another foreign
    language without an explicit dubbing marker counts as VOSE: Yelmo labelled
    those "INGLÉS SUBTITULADO EN ESPAÑOL (VOSE)" on the old site, and showing
    a film undubbed without subtitles is not something it does.
    """
    text = f"{language} {display}".lower()
    tokens = set(re.findall(r"[a-zà-ü.]+", text))
    if "vose" in text or "v.o.s.e" in text or "subt" in text or tokens & {"vos", "sub", "subs"}:
        return "VOSE"
    if "valen" in text or tokens & {"val", "va", "vlc"}:
        return "VAL"
    if "doblad" in text or "español" in text or "castellano" in text or tokens & {"es", "esp", "spa", "spanish"}:
        return "ES"
    if "original" in text or "v.o." in text or tokens & {"vo"}:
        return "VO"
    if "ingl" in text or "english" in text or tokens & {"en", "eng", "ing"}:
        return "VOSE"
    return "ES"


def _parse_showtime(raw: str, fallback_date: str = "") -> "tuple[str, str] | None":
    """Return (YYYY-MM-DD, HH:MM) in Madrid time for an API datetime.

    A value with an offset or 'Z' is converted to Europe/Madrid; a naive one
    is taken as already local, which is how the site displays it.
    """
    s = (raw or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(MADRID)
        return dt.date().isoformat(), dt.strftime("%H:%M")
    except ValueError:
        pass
    m = re.search(r"(\d{1,2}):(\d{2})", s)
    d = re.search(r"\d{4}-\d{2}-\d{2}", s) or re.search(r"\d{4}-\d{2}-\d{2}", fallback_date or "")
    if not m or not d:
        return None
    return d.group(0), f"{int(m.group(1)):02d}:{m.group(2)}"


def _extract_showtimes(schedule: dict, title: str, target_dates: set, url: str) -> "tuple[list[dict], set]":
    """Flatten a billboard / billboardOrderLinking payload into showtime rows.

    Returns the rows plus the set of (language, displayLanguage) pairs seen,
    which run.py surfaces in diagnostics so a wrong language guess is visible.
    """
    rows, langs, seen = [], set(), set()
    for sched in (schedule or {}).get("schedules") or []:
        for day in sched.get("dates") or []:
            for lang in day.get("languages") or []:
                code, display = lang.get("language") or "", lang.get("displayLanguage") or ""
                langs.add((code, display))
                label = _detect_language(code, display)
                for st in lang.get("showtimes") or []:
                    parsed = _parse_showtime(st.get("datetime") or "", day.get("date") or "")
                    if not parsed or parsed[0] not in target_dates:
                        continue
                    key = (parsed, label)
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append({
                        "title":    title,
                        "language": label,
                        "date":     parsed[0],
                        "time":     parsed[1],
                        "url":      url,
                    })
    return rows, langs


class _Api:
    """Replays GraphQL calls from inside the browser context that loaded the site."""

    def __init__(self, ctx, headers: dict):
        self.ctx = ctx
        self.headers = headers

    def query(self, operation: str, query: str, variables: dict) -> dict:
        body = {"operationName": operation, "variables": variables, "query": query}
        resp = self.ctx.request.post(
            BILLBOARDS, data=json.dumps(body), headers=self.headers, timeout=30000,
        )
        if resp.status != 200:
            raise RuntimeError(f"{operation}: HTTP {resp.status} {resp.text()[:200]}")
        payload = resp.json()
        if payload.get("errors") and not payload.get("data"):
            raise RuntimeError(f"{operation}: {payload['errors'][0].get('message', payload['errors'])}")
        return payload.get("data") or {}


def _open_api(p):
    """Load the site, wait for its own billboard request, and copy its headers."""
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(user_agent=_UA, locale="es-ES")
    page = ctx.new_page()
    captured: dict = {}

    def on_request(req):
        if not captured and req.url.startswith(BILLBOARDS) and req.method == "POST":
            captured.update(req.all_headers())

    page.on("request", on_request)
    status = None
    try:
        resp = page.goto(SITE_URL, timeout=45000)
        status = resp.status if resp else None
        for _ in range(40):
            if captured:
                break
            page.wait_for_timeout(500)
    except Exception as e:
        browser.close()
        raise RuntimeError(f"Yelmo site load failed (status={status}): {e}") from e

    if status and status >= 400:
        browser.close()
        raise RuntimeError(f"Yelmo site load failed: HTTP {status} at {page.url}")

    headers = {k: v for k, v in captured.items() if not k.startswith(":") and k.lower() not in _DROP_HEADERS}
    if not headers:
        # The site never called the API on its own; fall back to the minimum
        # a browser fetch from cineyelmo.com would send.
        _log("  [yelmo] no billboard request seen on the homepage; using default headers")
        headers = {"content-type": "application/json", "origin": "https://cineyelmo.com",
                   "referer": SITE_URL, "user-agent": _UA}
    return browser, _Api(ctx, headers)


def scrape() -> "tuple[list[dict], list[str]]":
    today = date.today()
    target_dates = {(today + timedelta(days=i)).isoformat() for i in range(7)}
    results: list[dict] = []
    diags: list[str] = []

    with sync_playwright() as p:
        browser, api = _open_api(p)
        try:
            # ── Phase 1: movies scheduled at Campanar ─────────────────────
            movies: dict[str, str] = {}
            for category in ("now-playing", "pre-sale"):
                data = api.query("Movies", _MOVIES_QUERY, {
                    "countryId": COUNTRY_ID, "category": category, "cinemas": CINEMA_ID,
                    "limit": 100, "isAbsoluteMediaPath": True,
                })
                for edge in ((data.get("movies") or {}).get("edges") or []):
                    node = edge.get("node") or {}
                    if node.get("id") and node.get("name"):
                        movies.setdefault(node["id"], node["name"].strip())
            if not movies:
                raise RuntimeError(f"Yelmo: Movies returned nothing for {CINEMA_ID}")
            _log(f"  [yelmo] {len(movies)} movies listed for {CINEMA_ID}")

            # ── Phase 2: sessions per movie ───────────────────────────────
            use_order_linking = True
            errors: list[str] = []
            langs_seen: set = set()
            for movie_id, title in movies.items():
                try:
                    if use_order_linking:
                        try:
                            data = api.query("BillboardOrderLinking", _ORDER_LINKING_QUERY, {
                                "countryId": COUNTRY_ID, "movieId": movie_id, "cinemaId": CINEMA_ID,
                                "controlTime": 0, "isAbsoluteExperiencePath": False,
                            })
                            schedule = data.get("billboardOrderLinking")
                        except RuntimeError as e:
                            # Only switch for good if the operation itself is
                            # unknown here; a per-movie failure is just logged.
                            if "Cannot query field" not in str(e) and "Unknown" not in str(e):
                                raise
                            _log(f"  [yelmo] BillboardOrderLinking refused ({e}); using Billboard")
                            use_order_linking = False
                    if not use_order_linking:
                        data = api.query("Billboard", _BILLBOARD_QUERY, {
                            "countryId": COUNTRY_ID, "movieId": movie_id,
                            "cinemas": CINEMA_ID, "timezone": "Europe/Madrid",
                        })
                        schedule = data.get("billboard")
                except Exception as e:
                    errors.append(f"{title}: {e}")
                    continue
                rows, langs = _extract_showtimes(schedule, title, target_dates, SITE_URL)
                langs_seen |= langs
                results.extend(rows)

            diags.append(f"languages seen (code, display): {sorted(langs_seen)}")
            if errors:
                _log(f"  ⚠ Yelmo: {len(errors)} schedule requests failed, first: {errors[0]}")
                diags.append(f"{len(errors)} schedule requests failed, first: {errors[0]}")
                if len(errors) == len(movies):
                    raise RuntimeError(f"Yelmo: every schedule request failed, first: {errors[0]}")
        finally:
            browser.close()

    return results, diags
