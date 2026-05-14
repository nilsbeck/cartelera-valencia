"""
Run: python scraper/yelmo_diag.py
Prints the raw HTML of every .horarioExp block for today's Yelmo cartelera page
so we can see exactly what language label structure we're dealing with.
"""
from datetime import date
from playwright.sync_api import sync_playwright

BASE_URL  = "https://www.yelmocines.es"
CITY_SLUG = "valencia/mercado-de-campanar"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(user_agent=(
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ))
    url = f"{BASE_URL}/cartelera/{CITY_SLUG}?fecha={date.today().isoformat()}"
    page.goto(url, timeout=30000)
    page.wait_for_selector("section#now__city article", timeout=15000)

    for article in page.query_selector_all("section#now__city article")[:3]:
        title = (article.query_selector("h3") or article).inner_text().split("\n")[0].strip()
        print(f"\n{'='*60}")
        print(f"MOVIE: {title}")
        for i, vb in enumerate(article.query_selector_all(".horarioExp")):
            print(f"\n  .horarioExp #{i}:")
            # Show direct-child span texts
            spans = page.evaluate("""el => {
                return Array.from(el.querySelectorAll(':scope > span'))
                    .map(s => s.innerText.trim());
            }""", vb)
            print(f"    direct child spans: {spans}")
            # Show first-span (current selector)
            first_span = vb.query_selector("span")
            print(f"    first span anywhere: {first_span.inner_text().strip() if first_span else None}")
            # Show all anchor texts
            anchors = [a.inner_text().strip()[:20] for a in vb.query_selector_all("a")]
            print(f"    anchors: {anchors}")

    browser.close()
