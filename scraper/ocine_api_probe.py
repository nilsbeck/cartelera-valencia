"""
One-off diagnostic for Ocine's ticketing API. Not used by run.py.

ocinepremiumaqua.es was rebuilt (base44 site builder) around 2026-10; the
film tiles the scraper read are gone. Showtimes now live in the ticketing app
at tickets.ocinepremiumaqua.es, which talks to a JSON API:

    POST /api/v1/init      {idioma, urlWeb, …}       → {token, …}
    POST /api/v1/sessions  {token, idioma, urlWeb}   → {pelicules: [...]}

site_probe.py captured those two calls but cut each body at 3000 chars, most
of which was a base64 poster. This prints the full structure of the answers
with long strings shortened, then opens one film's page in the ticketing app
and records every API call it makes, so the scraper can be written against
the real shapes. Run from the home server (the origin blocks cloud IPs):

    python ocine_api_probe.py > ocine-api.txt
"""

import json

from playwright.sync_api import sync_playwright

TICKETS = "https://tickets.ocinepremiumaqua.es"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0 Safari/537.36"
)

FETCH_JS = """async ({url, body}) => {
    const r = await fetch(url, {method: 'POST', headers: {'content-type': 'application/json'}, body});
    return {status: r.status, text: await r.text()};
}"""


def shorten(value, limit=160):
    """Copy of a JSON value with long strings cut, so base64 posters don't drown the output."""
    if isinstance(value, dict):
        return {k: shorten(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [shorten(v, limit) for v in value]
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"…<{len(value)} chars>"
    return value


def post(page, path, body):
    resp = page.evaluate(FETCH_JS, {"url": TICKETS + path, "body": json.dumps(body)})
    print(f"\n=== POST {path} -> {resp['status']}")
    try:
        return json.loads(resp["text"])
    except ValueError:
        print("non-JSON:", resp["text"][:500])
        return None


def main():
    calls = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA, locale="es-ES")

        def on_response(resp):
            if "/api/" in resp.url:
                try:
                    body = resp.text()
                except Exception:
                    body = ""
                calls.append((resp.request.method, resp.url, resp.request.post_data or "", body))

        page.on("response", on_response)
        page.goto(f"{TICKETS}/?nocap=1#/", timeout=45000)
        page.wait_for_timeout(6000)

        url_web = f"{TICKETS}/?nocap=1#/SeleccioPelicula"
        init = post(page, "/api/v1/init", {
            "idioma": "es", "plan": None, "butaques": None, "nume": None,
            "urlconfirmation": None, "urlinici": None, "deluxe": None, "app": None,
            "cli": None, "urlWeb": f"{TICKETS}/?nocap=1#/", "dataSessio": None,
            "horaSessio": None, "extern": None, "nomesbar": None,
        })
        print(json.dumps(shorten(init), ensure_ascii=False, indent=1)[:3000])
        token = (init or {}).get("token")
        if not token:
            print("no token; stopping")
            browser.close()
            return

        sessions = post(page, "/api/v1/sessions", {"token": token, "idioma": "es", "urlWeb": url_web})
        films = (sessions or {}).get("pelicules") or []
        print("top-level keys:", sorted((sessions or {}).keys()))
        print("film count:", len(films))
        if films:
            print("\n--- first film, full structure (long strings shortened):")
            print(json.dumps(shorten(films[0]), ensure_ascii=False, indent=1)[:12000])
            print("\n--- all films (id, title, keys with list values and their lengths):")
            for f in films:
                lists = {k: len(v) for k, v in f.items() if isinstance(v, list)}
                print("  ", f.get("id"), "|", f.get("titol"), "|", lists)
        rest = {k: v for k, v in (sessions or {}).items() if k != "pelicules"}
        if rest:
            print("\n--- other top-level fields:")
            print(json.dumps(shorten(rest), ensure_ascii=False, indent=1)[:6000])

        # What the app itself requests when a film is opened.
        if films:
            film_id = films[0].get("id")
            calls.clear()
            page.goto(f"{TICKETS}/?nocap=1#/DetallPelicula/{film_id}", timeout=45000)
            page.wait_for_timeout(8000)
            print(f"\n######## API calls when opening DetallPelicula/{film_id}: {len(calls)}")
            for method, url, post_data, body in calls:
                print(f"\n--- {method} {url}")
                if post_data:
                    print("POST:", post_data[:1500])
                try:
                    print("RESPONSE:", json.dumps(shorten(json.loads(body)), ensure_ascii=False)[:5000])
                except ValueError:
                    print("RESPONSE (raw):", body[:800])
            print("\nVISIBLE TEXT:", page.evaluate("() => document.body.innerText")[:3000])
        browser.close()


if __name__ == "__main__":
    main()
