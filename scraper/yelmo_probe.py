"""
One-off diagnostic for Yelmo's move to cineyelmo.com. Not used by run.py.

Since 2026-10-24 yelmocines.es redirects to cineyelmo.com, a Next.js app whose
data all comes from a GraphQL API at api-g.cinepolis.com. Both sit behind a
Cloudflare WAF that blocks datacenter IPs (GitHub runners, cloud dev boxes),
so this has to run from the home server. It records what the replacement
scraper needs:

  * every request to api-g.cinepolis.com with its POST body (the GraphQL
    query and variables) and the response,
  * the GraphQL operations compiled into the site's JavaScript,
  * the Valencia city / Mercado de Campanar cinema ids,
  * what happens on the Campanar cinema page and on a movie page.

Usage (output goes to stdout; redirect it to a file):
    python yelmo_probe.py > yelmo-probe.txt
"""

import json
import re
from playwright.sync_api import sync_playwright

START = "https://cineyelmo.com/es"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Responses for these get printed in full; everything else is truncated.
FULL = re.compile(r"showtime|session|schedule|horario|funcion|cinema", re.I)

api_calls: list[dict] = []


def on_response(resp):
    req = resp.request
    if "api-g.cinepolis.com" not in req.url:
        return
    try:
        body = resp.text()
    except Exception:
        body = "<unreadable>"
    api_calls.append({
        "method": req.method,
        "url": req.url,
        "post": req.post_data or "",
        "status": resp.status,
        "body": body,
    })


def section(title):
    print(f"\n\n################ {title}", flush=True)


def dump_new_calls(start):
    for c in api_calls[start:]:
        post = c["post"]
        wanted = FULL.search(post) or FULL.search(c["url"])
        print(f"\n--- {c['method']} {c['url']} -> {c['status']}")
        print("POST:", post[:6000])
        print("RESPONSE:", c["body"][: 20000 if wanted else 1500])
    return len(api_calls)


def same_origin_links(page):
    return page.evaluate(r"""() => [...new Set(Array.from(document.querySelectorAll('a[href]'))
        .map(a => a.href).filter(h => h.startsWith(location.origin) && !h.includes('#')))]""")


def visit(page, url, label, seen):
    section(f"{label}: {url}")
    try:
        r = page.goto(url, timeout=45000)
        page.wait_for_timeout(7000)
        print("STATUS:", r.status if r else None, "FINAL:", page.url, "TITLE:", page.title())
        print("BODY TEXT:", page.evaluate("() => document.body.innerText")[:3000])
    except Exception as e:
        print("ERROR:", e)
    return dump_new_calls(seen)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, locale="es-ES")
        page = ctx.new_page()
        page.on("response", on_response)

        section("HOME")
        r = page.goto(START, timeout=45000)
        print("STATUS:", r.status if r else None, "FINAL:", page.url)
        page.wait_for_timeout(6000)
        try:
            page.get_by_text("Permitir todas", exact=True).click(timeout=3000)
        except Exception:
            pass

        nd = page.evaluate("() => { const e = document.getElementById('__NEXT_DATA__'); return e ? e.textContent : null; }")
        print("__NEXT_DATA__:", (nd or "none")[:3000])
        links = same_origin_links(page)
        print(f"SAME-ORIGIN LINKS ({len(links)}):")
        for l in links[:120]:
            print("  ", l)
        seen = dump_new_calls(0)

        # ── GraphQL operations compiled into the JS bundles ─────────────
        section("GRAPHQL OPERATIONS IN JS")
        scripts = page.evaluate("() => Array.from(document.scripts).map(s => s.src).filter(Boolean)")
        if nd:
            try:
                build_id = json.loads(nd).get("buildId")
                bm = ctx.request.get(f"https://cineyelmo.com/_next/static/{build_id}/_buildManifest.js")
                print("BUILD MANIFEST:", bm.text()[:4000])
            except Exception as e:
                print("build manifest error:", e)
        ops: dict[str, str] = {}
        for src in scripts:
            try:
                js = ctx.request.get(src, timeout=30000).text()
            except Exception:
                continue
            for m in re.finditer(r"(query|mutation)\s+([A-Za-z0-9_]+)\s*[({]", js):
                name = m.group(2)
                if name not in ops:
                    ops[name] = js[m.start(): m.start() + 2500]
            for m in re.finditer(r'https://api-g\.cinepolis\.com/[A-Za-z0-9/_.-]+', js):
                ops.setdefault("URL " + m.group(0), "")
        print("OPERATION NAMES:", sorted(ops))
        for name, text in ops.items():
            if text and FULL.search(name + text[:300]):
                print(f"\n=== {name}\n{text}")

        # ── Valencia / Campanar ids from the locations response ─────────
        section("VALENCIA")
        cinema_id = city_id = None
        for c in api_calls:
            if "locations/graphql" not in c["url"] or '"cities"' not in c["body"]:
                continue
            try:
                for edge in json.loads(c["body"])["data"]["cities"]["edges"]:
                    node = edge["node"]
                    if "valencia" in node["id"].lower():
                        print(json.dumps(node, ensure_ascii=False, indent=1))
                        for cin in node.get("cinemas", []):
                            if "campanar" in (cin["id"] + cin["name"]).lower():
                                cinema_id, city_id = cin["id"], node["id"]
            except Exception as e:
                print("parse error:", e)
        print("CAMPANAR:", city_id, cinema_id)

        # ── Candidate cinema pages ─────────────────────────────────────
        if cinema_id:
            for path in (f"/es/cines/{cinema_id}", f"/es/cine/{cinema_id}",
                         f"/es/cartelera/{city_id}/{cinema_id}", f"/es/cartelera/{cinema_id}",
                         f"/es/horarios/{city_id}/{cinema_id}", f"/es/horarios/{cinema_id}"):
                seen = visit(page, "https://cineyelmo.com" + path, "CINEMA CANDIDATE", seen)
                if "404" not in page.title() and cinema_id in page.url:
                    links = same_origin_links(page)
                    print("LINKS ON CINEMA PAGE:", links[:80])
                    break

        # ── One movie page, which is where sessions are usually shown ───
        movie = next((l for l in links if re.search(r"pelicula|movie|sinopsis|film", l)), None)
        if movie:
            seen = visit(page, movie, "MOVIE", seen)
            print("SELECTS/BUTTONS:", page.evaluate(r"""() => Array.from(document.querySelectorAll('select, button'))
                .map(e => e.tagName + ': ' + (e.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60)).slice(0, 60)"""))
            # Try to pick Valencia → Campanar if the page asks for a cinema.
            for label in ("Valencia", "Mercado de Campanar", "Campanar"):
                try:
                    page.get_by_text(label, exact=False).first.click(timeout=3000)
                    page.wait_for_timeout(4000)
                    print("clicked", label)
                except Exception:
                    pass
            print("AFTER CLICKS URL:", page.url)
            print("BODY TEXT:", page.evaluate("() => document.body.innerText")[:4000])
            seen = dump_new_calls(seen)

        browser.close()


if __name__ == "__main__":
    main()
