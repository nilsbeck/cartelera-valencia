"""
Cine Yelmo Valencia scraper — Mercado de Campanar location only.

Since 2026-10-24 yelmocines.es redirects to cineyelmo.com, a Next.js app that
renders nothing server-side and gets all its data from Cinépolis's GraphQL
API at api-g.cinepolis.com. We query that API directly instead of the DOM.

Both hosts sit behind a Cloudflare WAF that 403s datacenter IPs and non-browser
clients, so requests go through Playwright: load the homepage once (which
clears Cloudflare and makes the site send its own billboard request), copy the
headers of that request, and replay them with fetch() inside that page so our
calls leave through Chrome's own network stack, exactly like the site's.
(Playwright's ctx.request client is not Chrome and gets a Cloudflare 403.)

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
from datetime import date, datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

SITE_URL     = "https://cineyelmo.com/es"
BILLBOARDS   = "https://api-g.cinepolis.com/v2/billboards/graphql"
COUNTRY_ID   = "ES"
CINEMA_ID    = "mercado-de-campanar-valencia"

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

# Headers the browser computes per request, or that fetch() refuses to set.
# Tracing headers are dropped too: the site's Dynatrace agent wraps fetch()
# and adds its own, and a header the API's CORS policy does not list makes the
# preflight fail ("TypeError: Failed to fetch", seen on the first live run).
_DROP_HEADERS = {"content-length", "host", "cookie", "accept-encoding", "connection",
                 "origin", "referer", "user-agent", "accept-language", "accept",
                 "priority", "traceparent", "tracestate"}

_FETCH_JS = """async ({url, body, headers}) => {
    const r = await fetch(url, {method: 'POST', headers, body});
    return {status: r.status, text: await r.text()};
}"""


def _log(msg: str) -> None:
    print(msg, flush=True)


def _detect_language(language: str, display: str) -> str:
    """Map the API's (language, displayLanguage) pair to VOSE / VAL / VO / ES.

    Codes seen on the first live run (2026-10-26): ("ESP", "ESPAÑOL"),
    ("SUB ESP", "SUB ESP") and ("COR", "COR"), the last being Korean, used for
    the K-pop concert films. Anything else is matched on substrings of both
    fields. A foreign language without an explicit dubbing marker counts as
    VOSE: Yelmo labelled those "INGLÉS SUBTITULADO EN ESPAÑOL (VOSE)" on the
    old site, and showing a film undubbed without subtitles is not something
    it does.
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
    if (any(w in text for w in ("ingl", "english", "corea", "japon", "franc", "italia", "alem"))
            or tokens & {"en", "eng", "ing", "cor", "kor", "jap", "jpn", "fra", "ita", "ale"}):
        return "VOSE"
    return "ES"


def _last_sunday(year: int, month: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - 6) % 7)


def _to_madrid(dt: datetime) -> datetime:
    """Convert an aware datetime to naive Madrid local time.

    Done by hand rather than with zoneinfo: the Playwright Docker image the
    scraper runs in ships no tz database, so ZoneInfo("Europe/Madrid") raises.
    Peninsular Spain is CET (UTC+1), CEST (UTC+2) from 01:00 UTC on the last
    Sunday of March to 01:00 UTC on the last Sunday of October (EU rule).
    """
    utc = dt.astimezone(timezone.utc).replace(tzinfo=None)
    start = datetime.combine(_last_sunday(utc.year, 3), datetime.min.time()) + timedelta(hours=1)
    end = datetime.combine(_last_sunday(utc.year, 10), datetime.min.time()) + timedelta(hours=1)
    return utc + timedelta(hours=2 if start <= utc < end else 1)


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
            dt = _to_madrid(dt)
        return dt.date().isoformat(), dt.strftime("%H:%M")
    except ValueError:
        pass
    m = re.search(r"(\d{1,2}):(\d{2})", s)
    d = re.search(r"\d{4}-\d{2}-\d{2}", s) or re.search(r"\d{4}-\d{2}-\d{2}", fallback_date or "")
    if not m or not d:
        return None
    return d.group(0), f"{int(m.group(1)):02d}:{m.group(2)}"


def _missing_dates(schedule: dict, target_dates: set) -> "list[str]":
    """Target dates the payload lists as having sessions but carries none for.

    BillboardOrderLinking returned only today's sessions on the first live run
    (48 showtimes across 56 films for the week) while its top-level `dates`
    field is the list of days with sessions, the one the site's date picker
    shows. A non-empty answer here means the other operation should be asked.
    """
    if not schedule:
        return []
    listed = {m for d in schedule.get("dates") or [] for m in re.findall(r"\d{4}-\d{2}-\d{2}", str(d))}
    covered = {
        m
        for sched in schedule.get("schedules") or []
        for day in sched.get("dates") or []
        for m in re.findall(r"\d{4}-\d{2}-\d{2}", str(day.get("date") or ""))
    }
    return sorted((listed - covered) & target_dates)


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
    """Replays GraphQL calls with fetch() from inside the browser.

    Three ways of sending, tried in order until one gets an HTTP answer, which
    is then kept for the rest of the run:
      1. from the cineyelmo.com page, with the headers copied from the site;
      2. from the same page with only content-type (if a copied header is
         what failed the CORS preflight);
      3. from a page on api-g.cinepolis.com itself, where the call is
         same-origin and CORS does not apply.
    """

    def __init__(self, ctx, page, headers: dict):
        self.ctx = ctx
        self.page = page
        self.headers = headers
        self.chosen = None
        self.api_page = None

    def _send(self, how: int, body: str) -> dict:
        if how == 3:
            if self.api_page is None:
                self.api_page = self.ctx.new_page()
                self.api_page.goto(BILLBOARDS, timeout=45000)
            return self.api_page.evaluate(_FETCH_JS, {
                "url": BILLBOARDS, "body": body, "headers": {"content-type": "application/json"}})
        headers = self.headers if how == 1 else {"content-type": "application/json"}
        return self.page.evaluate(_FETCH_JS, {"url": BILLBOARDS, "body": body, "headers": headers})

    def query(self, operation: str, query: str, variables: dict) -> dict:
        body = json.dumps({"operationName": operation, "variables": variables, "query": query})
        if self.chosen:
            resp = self._send(self.chosen, body)
        else:
            failures = []
            for how in (1, 2, 3):
                try:
                    resp = self._send(how, body)
                except Exception as e:
                    failures.append(f"way {how}: {str(e).splitlines()[0][:160]}")
                    continue
                if resp["status"] == 200:
                    self.chosen = how
                    _log(f"  [yelmo] API calls work via way {how}")
                    break
                failures.append(f"way {how}: HTTP {resp['status']}")
            else:
                raise RuntimeError(f"{operation}: no way of calling the API worked: {'; '.join(failures)}")
        if resp["status"] != 200:
            raise RuntimeError(f"{operation}: HTTP {resp['status']} {resp['text'][:200]}")
        payload = json.loads(resp["text"])
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

    headers = {k: v for k, v in captured.items()
               if not k.startswith(":") and not k.lower().startswith(("sec-", "x-dt"))
               and k.lower() not in _DROP_HEADERS}
    if not captured:
        # The site never called the API on its own; fetch() from the page
        # still supplies origin, referer and user agent itself.
        _log("  [yelmo] no billboard request seen on the homepage; using default headers")
    # Names only: values may carry session ids or tokens and must not be logged.
    _log(f"  [yelmo] site request headers: {sorted(captured)}; replaying: {sorted(headers)}")
    headers.setdefault("content-type", "application/json")
    return browser, _Api(ctx, page, headers)


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
            billboard_ok = True
            errors: list[str] = []
            langs_seen: set = set()
            filled = 0
            for movie_id, title in movies.items():
                payloads = []
                schedule = None
                try:
                    if use_order_linking:
                        try:
                            data = api.query("BillboardOrderLinking", _ORDER_LINKING_QUERY, {
                                "countryId": COUNTRY_ID, "movieId": movie_id, "cinemaId": CINEMA_ID,
                                "controlTime": 0, "isAbsoluteExperiencePath": False,
                            })
                            schedule = data.get("billboardOrderLinking")
                            payloads.append(schedule)
                        except RuntimeError as e:
                            # Only switch for good if the operation itself is
                            # unknown here; a per-movie failure is just logged.
                            if "Cannot query field" not in str(e) and "Unknown" not in str(e):
                                raise
                            _log(f"  [yelmo] BillboardOrderLinking refused ({e}); using Billboard")
                            use_order_linking = False
                    if billboard_ok and (not use_order_linking or _missing_dates(schedule, target_dates)):
                        try:
                            data = api.query("Billboard", _BILLBOARD_QUERY, {
                                "countryId": COUNTRY_ID, "movieId": movie_id,
                                "cinemas": CINEMA_ID, "timezone": "Europe/Madrid",
                            })
                            payloads.append(data.get("billboard"))
                            if use_order_linking:
                                filled += 1
                        except RuntimeError as e:
                            if not use_order_linking or ("Cannot query field" not in str(e) and "Unknown" not in str(e)):
                                raise
                            _log(f"  [yelmo] Billboard refused ({e}); later days may be missing")
                            billboard_ok = False
                except Exception as e:
                    errors.append(f"{title}: {e}")
                    continue
                seen = set()
                for payload in payloads:
                    rows, langs = _extract_showtimes(payload, title, target_dates, SITE_URL)
                    langs_seen |= langs
                    for r in rows:
                        key = (r["date"], r["time"], r["language"])
                        if key not in seen:
                            seen.add(key)
                            results.append(r)

            per_day = {d: sum(1 for r in results if r["date"] == d) for d in sorted(target_dates)}
            _log(f"  [yelmo] showtimes per day: {per_day}; Billboard asked for {filled} films")
            diags.append(f"showtimes per day: {per_day}")
            diags.append(f"languages seen (code, display): {sorted(langs_seen)}")
            if errors:
                _log(f"  ⚠ Yelmo: {len(errors)} schedule requests failed, first: {errors[0]}")
                diags.append(f"{len(errors)} schedule requests failed, first: {errors[0]}")
                if len(errors) == len(movies):
                    raise RuntimeError(f"Yelmo: every schedule request failed, first: {errors[0]}")
        finally:
            browser.close()

    return results, diags
