"""
Yelmo Cines Valencia scraper — Mercado de Campanar location only.
URL: https://www.yelmocines.es/cartelera/valencia/mercado-de-campanar?fecha=YYYY-MM-DD

DOM structure (client-rendered — Playwright MUST wait for JS):
  section#now__city.listaCarteleraHorario  (empty placeholder in raw HTML)
    article                                → one movie
      .descripcion header h3              → title
      .horarioExp                         → one version/language group
        span                              → language label (first span)
        a > time  OR  time               → showtime element
          text / datetime attr            → "HH:MM"

One page request per date (7 days). Each page URL filters by ?fecha=.
"""

from datetime import date, timedelta
from playwright.sync_api import sync_playwright

BASE_URL  = "https://www.yelmocines.es"
CITY_SLUG = "valencia/mercado-de-campanar"


def _is_novapark_href(href: str) -> bool:
    """Return True if a session booking link belongs to Yelmo NovaPark."""
    return "novapark" in href.lower()


def scrape() -> list[dict]:
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ))

        for offset in range(7):
            target   = date.today() + timedelta(days=offset)
            date_str = target.isoformat()
            url      = f"{BASE_URL}/cartelera/{CITY_SLUG}?fecha={date_str}"

            try:
                page.goto(url, timeout=30000)
                # The page is JS-rendered; wait until at least one article appears
                # (falls back gracefully with timeout if no films are listed)
                try:
                    page.wait_for_selector(
                        "section#now__city article",
                        timeout=15000,
                    )
                except Exception:
                    # No films for this date (or selector changed) — move on
                    continue

                movie_blocks = page.query_selector_all("section#now__city article")

                for block in movie_blocks:
                    title_el = block.query_selector(".descripcion header h3")
                    if not title_el:
                        title_el = block.query_selector("h3")
                    if not title_el:
                        continue
                    title = title_el.inner_text().strip()
                    if not title:
                        continue

                    # Each .horarioExp row = one language version
                    version_blocks = block.query_selector_all(".horarioExp")

                    if version_blocks:
                        for vb in version_blocks:
                            # Walk all spans; use the first one that doesn't look
                            # like a time (HH:MM) — avoids picking up icon/time
                            # spans nested inside <a> elements before the label.
                            language = vb.evaluate("""el => {
                                for (const s of el.querySelectorAll('span')) {
                                    const t = s.textContent.trim();
                                    if (t && !/^\\d{1,2}:\\d{2}/.test(t)) return t;
                                }
                                return '';
                            }""") or "es"

                            # Times: prefer <a><time>HH:MM</time></a>,
                            # fall back to bare <time> elements
                            time_anchors = vb.query_selector_all("a")
                            if time_anchors:
                                for a_el in time_anchors:
                                    href = a_el.get_attribute("href") or url
                                    # Each session link encodes the cinema; skip NovaPark.
                                    if _is_novapark_href(href):
                                        continue
                                    time_el = a_el.query_selector("time")
                                    if time_el:
                                        time_text = (
                                            time_el.get_attribute("datetime")
                                            or time_el.inner_text()
                                        ).strip()
                                    else:
                                        time_text = a_el.inner_text().strip()
                                    if not time_text or ":" not in time_text:
                                        continue
                                    results.append({
                                        "title":    title,
                                        "language": language,
                                        "date":     date_str,
                                        "time":     time_text[:5],  # "HH:MM"
                                        "url":      href if href.startswith("http") else BASE_URL + href,
                                    })
                            else:
                                # Bare <time> elements (no wrapping <a>)
                                for time_el in vb.query_selector_all("time"):
                                    time_text = (
                                        time_el.get_attribute("datetime")
                                        or time_el.inner_text()
                                    ).strip()
                                    if not time_text or ":" not in time_text:
                                        continue
                                    results.append({
                                        "title":    title,
                                        "language": language,
                                        "date":     date_str,
                                        "time":     time_text[:5],
                                        "url":      url,
                                    })
                    else:
                        # Flat fallback: no .horarioExp, gather all <time> in block
                        for time_el in block.query_selector_all("time"):
                            time_text = (
                                time_el.get_attribute("datetime")
                                or time_el.inner_text()
                            ).strip()
                            if not time_text or ":" not in time_text:
                                continue
                            results.append({
                                "title":    title,
                                "language": "es",
                                "date":     date_str,
                                "time":     time_text[:5],
                                "url":      url,
                            })

            except Exception as e:
                print(f"  ⚠ Yelmo error on {date_str}: {e}")
                continue

        browser.close()

    return results
