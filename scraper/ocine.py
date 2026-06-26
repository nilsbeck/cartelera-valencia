"""
Ocine Premium Aqua Valencia scraper.
URL: https://www.ocinepremiumaqua.es/

The Joomla com_cines module pre-generates a static JSON file at
/components/com_cines/json/es_cartellera.json that contains all current
sessions for all films.  We fetch that file with plain requests — no
JavaScript rendering needed.

The JSON has two film arrays:
  data  — Spanish-dubbed versions  → language="es"
  vose  — original-language-with-Spanish-subtitles → language="vose"

Each film entry's Planificacions list contains one dict per session with
plan_data (YYYY-MM-DD) and plan_horainici (HH:MM:SS).

Ocine's server blocks cloud-datacenter IPs (GitHub Actions, AWS, etc.) at
the TCP level.  When OCINE_PROXY_URL is set, requests are routed through the
EU fetch-proxy on Render.  The proxy runs on Render's free tier which sleeps
after inactivity, so the first call sends a cheap warm-up GET / before the
real fetch.  That way the cold-start wait is absorbed by the warmup, and the
actual JSON read completes quickly once the proxy is awake.
"""

import os
import re
import requests
from datetime import date, timedelta

BASE_URL = "https://www.ocinepremiumaqua.es"
_JSON_PATH = "/components/com_cines/json/es_cartellera.json"
_CARTELLERA_JSON = BASE_URL + _JSON_PATH

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0 Safari/537.36"
)

_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json,text/html,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9",
    "Referer": BASE_URL + "/",
}

# Tags that appear in film titles but describe format/version, not the film.
# "(VOSE)" is already captured in the language field so always strip it;
# the rest (ATMOS, URBAN, …) are format labels that would break TMDB lookup.
_FORMAT_TAG_RE = re.compile(
    r"\s*\(\s*(?:VOSE|V\.O\.S\.E\.?|ATMOS|URBAN|ICE|Screen\s*X|Premium)\s*\)\s*",
    re.IGNORECASE,
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


def _detect_lang(text: str) -> str:
    """Infer a raw language tag from a version-label string."""
    t = text.lower()
    for token, lang in _LANG_TOKENS:
        if token in t:
            return lang
    return "es"


def _dates_until_next_thursday() -> list[str]:
    """ISO date strings from today through the upcoming Thursday (inclusive)."""
    today = date.today()
    days_ahead = (3 - today.weekday()) % 7  # Thursday = weekday 3
    if days_ahead == 0:
        days_ahead = 7
    end = today + timedelta(days=days_ahead)
    result: list[str] = []
    d = today
    while d <= end:
        result.append(d.isoformat())
        d += timedelta(days=1)
    return result


def _clean_title(raw: str) -> str:
    """Strip format/version tags from a film title."""
    t = _FORMAT_TAG_RE.sub(" ", raw).strip()
    return re.sub(r"\s{2,}", " ", t)


def _log(msg: str) -> None:
    print(msg, flush=True)


def _fetch_cartellera(proxy_url: "str | None", proxy_token: str) -> "dict | None":
    if proxy_url:
        proxy_url = proxy_url.rstrip("/")
        hdrs: dict[str, str] = {}
        if proxy_token:
            hdrs["X-Proxy-Token"] = proxy_token
        # Render free tier sleeps after inactivity; wake it with a cheap
        # health-check so the cold-start delay doesn't eat into our real fetch.
        try:
            requests.get(f"{proxy_url}/", headers=hdrs, timeout=70)
        except Exception:
            pass  # warmup is best-effort; proceed regardless
        try:
            r = requests.get(
                f"{proxy_url}/fetch",
                params={"url": _CARTELLERA_JSON},
                headers=hdrs,
                timeout=30,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            _log(f"  ⚠ Ocine JSON fetch (via proxy) failed: {e}")
            return None
    else:
        try:
            r = requests.get(_CARTELLERA_JSON, headers=_HEADERS, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            _log(f"  ⚠ Ocine JSON fetch failed: {e}")
            return None


def scrape() -> list[dict]:
    proxy_url   = os.environ.get("OCINE_PROXY_URL")
    proxy_token = os.environ.get("OCINE_PROXY_TOKEN", "")

    mode = "JSON via proxy" if proxy_url else "JSON direct"
    _log(f"  [ocine] fetch mode: {mode}")

    data = _fetch_cartellera(proxy_url, proxy_token)
    if not data:
        return []

    target_dates = set(_dates_until_next_thursday())
    results: list[dict] = []

    # "data" = ES dubbed, "vose" = original language + Spanish subtitles
    sources: list[tuple[str, list]] = [
        ("es",   data.get("data") or []),
        ("vose", data.get("vose") or []),
    ]

    for lang, films in sources:
        for film in films:
            raw_title = (film.get("peli_titol") or "").strip()
            if not raw_title:
                continue
            title = _clean_title(raw_title)
            film_id = film.get("peli_pelicula")
            film_url = f"{BASE_URL}/film-{film_id}/p" if film_id else BASE_URL

            for plan in (film.get("Planificacions") or []):
                plan_date = (plan.get("plan_data") or "").strip()
                if plan_date not in target_dates:
                    continue
                time_raw = (plan.get("plan_horainici") or "").strip()
                time_str = time_raw[:5]  # "HH:MM" from "HH:MM:SS"
                if not time_str or ":" not in time_str:
                    continue
                results.append({
                    "title":    title,
                    "language": lang,
                    "date":     plan_date,
                    "time":     time_str,
                    "url":      film_url,
                })

    _log(f"  [ocine] {len(results)} showtimes")
    return results
