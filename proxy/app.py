"""
Tiny HTTP fetch-proxy for cinema scrapers that hit EU-geofenced cinemas.

Deployed as a free Render web service in Frankfurt. The scraper running on
GitHub-hosted runners (US egress) calls /fetch?url=… and gets back the raw
upstream body, fetched from this server's EU IP.

Endpoints:
  GET /         — health check, returns {"ok": true}
  GET /fetch    — params: url=<encoded full URL>
                  headers: X-Proxy-Token: <shared secret> (if PROXY_TOKEN set)
                  returns the upstream response body and content-type as-is.

The host of the requested URL must appear in the ALLOWED_HOSTS env var
(comma-separated, no scheme). This prevents the service from being abused
as an open proxy.
"""

import os
from urllib.parse import urlparse

import requests
from flask import Flask, Response, abort, jsonify, request

ALLOWED_HOSTS = {
    h.strip().lower()
    for h in os.environ.get(
        "ALLOWED_HOSTS",
        "ocinepremiumaqua.es,www.ocinepremiumaqua.es",
    ).split(",")
    if h.strip()
}
PROXY_TOKEN = os.environ.get("PROXY_TOKEN", "")
TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "30"))

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/136.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

app = Flask(__name__)


@app.get("/")
def health():
    return jsonify({"ok": True, "allowed_hosts": sorted(ALLOWED_HOSTS)})


@app.get("/fetch")
def fetch():
    if PROXY_TOKEN and request.headers.get("X-Proxy-Token") != PROXY_TOKEN:
        abort(401)

    url = (request.args.get("url") or "").strip()
    if not url:
        abort(400, "missing url parameter")

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        abort(400, "scheme must be http or https")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        abort(403, f"host {host!r} not in allowlist")

    # Forward a couple of optional client-supplied headers if present, so the
    # scraper can pin a specific User-Agent or Referer without rebuilding the
    # proxy each time.
    headers = dict(_DEFAULT_HEADERS)
    for h in ("User-Agent", "Referer", "Accept-Language"):
        v = request.headers.get(f"X-Forward-{h}")
        if v:
            headers[h] = v

    try:
        r = requests.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
        return jsonify({"error": "upstream_failed", "detail": str(e)}), 502

    return Response(
        r.content,
        status=r.status_code,
        content_type=r.headers.get("Content-Type", "text/html; charset=utf-8"),
    )
