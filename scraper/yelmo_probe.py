"""
One-off diagnostic for Yelmo's move to cinepolis.com. Not used by run.py.

Since 2026-10-24 yelmocines.es redirects to https://cinepolis.com/es, which is
behind a Cloudflare WAF that blocks datacenter IPs (GitHub runners, cloud dev
boxes). It has to be run from the home server, which gets through. It records:

  * every JSON/XHR response the site makes (the cleanest thing to scrape is
    usually the API the SPA calls, not its HTML),
  * links mentioning Valencia / Campanar / movies,
  * the visible text of the Campanar cinema page, if one is found.

Usage (output goes to stdout; redirect it to a file):
    python yelmo_probe.py > yelmo-probe.txt
"""

import json
import re
from playwright.sync_api import sync_playwright

START = "https://cinepolis.com/es"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
INTERESTING = re.compile(r"valencia|campanar|cine|pelicula|cartelera|movie|film|sesion|horario", re.I)

seen_json: dict[str, str] = {}


def on_response(resp):
    try:
        ct = resp.headers.get("content-type", "")
        if "json" not in ct or resp.url in seen_json:
            return
        seen_json[resp.url] = resp.text()[:2500]
    except Exception:
        pass


def dump_page(page, label):
    print(f"\n######## {label}: {page.url}")
    print("TITLE:", page.title())
    links = page.evaluate(r"""() => [...new Set(Array.from(document.querySelectorAll('a[href]'))
        .map(a => a.href + '  |  ' + (a.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60)))]""")
    hits = [l for l in links if INTERESTING.search(l)]
    print(f"LINKS ({len(links)} total, {len(hits)} relevant):")
    for l in hits[:150]:
        print("  ", l)
    print("NEXT/NUXT:", page.evaluate(
        "() => ({next: !!document.getElementById('__NEXT_DATA__'), nuxt: !!window.__NUXT__})"))
    print("SELECTS:", page.evaluate(r"""() => Array.from(document.querySelectorAll('select'))
        .map(s => (s.id || s.name) + ': ' + Array.from(s.options).slice(0, 15).map(o => o.value + '=' + o.textContent.trim()).join(' ; '))"""))
    text = page.evaluate("() => document.body.innerText")
    print("BODY TEXT (first 4000 chars):")
    print(text[:4000])


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA, locale="es-ES")
        page.on("response", on_response)

        resp = page.goto(START, timeout=45000)
        print("START STATUS:", resp.status if resp else None)
        page.wait_for_timeout(8000)
        dump_page(page, "HOME")

        # Follow the first link that looks like the Campanar cinema, else Valencia.
        target = None
        for pattern in ("campanar", "valencia"):
            target = page.evaluate(
                "(p) => { const a = Array.from(document.querySelectorAll('a[href]'))"
                ".find(a => a.href.toLowerCase().includes(p) || (a.textContent||'').toLowerCase().includes(p));"
                " return a ? a.href : null; }",
                pattern,
            )
            if target:
                break
        if target:
            page.goto(target, timeout=45000)
            page.wait_for_timeout(8000)
            dump_page(page, "CINEMA")

            # One movie page from there, to see how sessions/versions are shown.
            movie = page.evaluate(
                r"""() => { const a = Array.from(document.querySelectorAll('a[href]'))
                    .find(a => /pelicula|movie|film|sinopsis/i.test(a.href)); return a ? a.href : null; }""")
            if movie:
                page.goto(movie, timeout=45000)
                page.wait_for_timeout(8000)
                dump_page(page, "MOVIE")
                print("TIME ELEMENTS:", json.dumps(page.evaluate(
                    r"""() => Array.from(document.querySelectorAll('time, [class*="session" i], [class*="horario" i], [class*="showtime" i]'))
                        .slice(0, 8).map(e => e.outerHTML.slice(0, 500))"""), indent=1, ensure_ascii=False))
        else:
            print("\nNo Campanar/Valencia link found on the home page.")

        print(f"\n######## JSON RESPONSES ({len(seen_json)})")
        for url, body in seen_json.items():
            print(f"\n--- {url}\n{body}")
        browser.close()


if __name__ == "__main__":
    main()
