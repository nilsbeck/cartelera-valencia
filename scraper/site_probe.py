"""
One-off diagnostic for a cinema site that stopped scraping. Not used by run.py.

Opens a URL in Playwright the way the scrapers do and prints what a scraper
needs to know to be rewritten:

  * the HTTP status, final URL (after redirects) and page title,
  * the visible text and the start of the rendered HTML,
  * links on the page,
  * every JSON / XHR response, with its request method, POST body and the
    names of its request headers (never their values, which can carry
    session ids or keys).

It has to run from the home server: most of these sites block datacenter
IPs. Written after Yelmo moved to cineyelmo.com (2026-10-24), where this kind
of capture is what showed the GraphQL API the new scraper reads.

Usage (output goes to stdout; redirect it to a file):
    python site_probe.py https://ocinepremiumaqua.es/ > probe.txt
"""

import json
import re
import sys

from playwright.sync_api import sync_playwright

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0 Safari/537.36"
)
STATIC = re.compile(r"\.(css|js|png|jpe?g|gif|svg|webp|woff2?|ttf|ico)(\?|$)", re.I)

calls: list[dict] = []


def on_response(resp):
    req = resp.request
    if req.resource_type not in ("xhr", "fetch") and "json" not in resp.headers.get("content-type", ""):
        return
    if STATIC.search(req.url):
        return
    try:
        body = resp.text()
    except Exception:
        body = "<unreadable>"
    calls.append({
        "method": req.method,
        "url": req.url,
        "status": resp.status,
        "type": resp.headers.get("content-type", ""),
        "post": req.post_data or "",
        "header_names": sorted(req.headers),
        "body": body,
    })


def main(url: str) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA, locale="es-ES")
        page.on("response", on_response)

        try:
            resp = page.goto(url, timeout=45000)
            print("STATUS:", resp.status if resp else None)
        except Exception as e:
            print("GOTO ERROR:", e)
        page.wait_for_timeout(12000)
        print("FINAL URL:", page.url)
        print("TITLE:", page.title())

        print("\n######## VISIBLE TEXT (first 4000 chars)")
        print(page.evaluate("() => document.body ? document.body.innerText : ''")[:4000])

        html = page.content()
        body = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>|<svg[\s\S]*?</svg>", "", html)
        i = body.find("<body")
        print(f"\n######## RENDERED HTML (body, scripts stripped, first 8000 of {len(body)} chars)")
        print(body[max(i, 0): max(i, 0) + 8000])

        print("\n######## SCRIPT SOURCES")
        for src in page.evaluate("() => Array.from(document.scripts).map(s => s.src).filter(Boolean)")[:40]:
            print("  ", src)

        links = page.evaluate(r"""() => [...new Set(Array.from(document.querySelectorAll('a[href]'))
            .map(a => a.href + '  |  ' + (a.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60)))]""")
        print(f"\n######## LINKS ({len(links)}, first 80)")
        for l in links[:80]:
            print("  ", l)

        print(f"\n######## JSON / XHR CALLS ({len(calls)})")
        for c in calls:
            print(f"\n--- {c['method']} {c['url']} -> {c['status']} ({c['type']})")
            print("REQUEST HEADER NAMES:", c["header_names"])
            if c["post"]:
                print("POST:", c["post"][:3000])
            print("RESPONSE:", c["body"][:3000])
        browser.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "https://ocinepremiumaqua.es/")
