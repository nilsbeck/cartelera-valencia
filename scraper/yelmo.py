"""
Yelmo Cines Valencia scraper — Mercado de Campanar location only.
URL: https://www.yelmocines.es/cartelera/valencia/mercado-de-campanar?fecha=YYYY-MM-DD

DOM structure (client-rendered — Playwright MUST wait for JS):
  section#now__city article              → one movie
    [data-cinema="mercado-de-campanar"]  → groups sessions for this cinema
      div                                → one showtime row
        label / span / p                 → language version label
        time                             → "HH:MM"
        a                                → booking link

One page request per date (7 days). Each page URL filters by ?fecha=.
"""

from datetime import date, timedelta
from playwright.sync_api import sync_playwright

BASE_URL  = "https://www.yelmocines.es"
CITY_SLUG = "valencia/mercado-de-campanar"


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
                try:
                    page.wait_for_selector(
                        "section#now__city article",
                        timeout=15000,
                    )
                except Exception:
                    continue

                # Sessions and their version <label>s hydrate after the
                # article shells appear. wait_for_selector returns as soon
                # as a (possibly empty) <label> node exists, so wait until
                # at least one label has actual text — otherwise every
                # session falls back to ES.
                try:
                    page.wait_for_function(
                        """() => {
                            const labels = document.querySelectorAll(
                                '[data-cinema="mercado-de-campanar"] label'
                            );
                            return Array.from(labels).some(
                                l => l.textContent.trim().length > 3
                            );
                        }""",
                        timeout=10000,
                    )
                except Exception:
                    pass  # legitimately no sessions for this day — keep going

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

                    # Each [data-cinema="mercado-de-campanar"] groups session rows for
                    # that location. Each direct child div is one showtime: it contains
                    # a language label, a <time> tag, and the booking <a>.
                    sessions = block.evaluate(r"""el => {
                        const out = [];

                        // Read the version from the <label> attached to a
                        // session. Labels contain a substring like "vose",
                        // "español", "vo", "castellano" etc. — match by
                        // contains and emit a canonical code so
                        // normalize_lang maps cleanly.
                        function detectLang(row) {
                            for (const label of row.querySelectorAll('label')) {
                                const text = (label.textContent || '').trim().toLowerCase();
                                if (!text) continue;
                                if (text.includes('vose') || text.includes('v.o.s.e')) return 'VOSE';
                                if (text.includes('español') || text.includes('castellano') || text.includes('doblad')) return 'ES';
                                if (text.includes('valencià') || text.includes('valenciano')) return 'VAL';
                                if (text.includes('original') || text.includes('v.o.') || /\bvo\b/.test(text)) return 'VO';
                            }
                            return '';
                        }

                        for (const outer of el.querySelectorAll('[data-cinema="mercado-de-campanar"]')) {
                            const rows = Array.from(outer.querySelectorAll(':scope > div'));
                            const candidates = rows.length ? rows : [outer];

                            for (const row of candidates) {
                                for (const time of row.querySelectorAll('time')) {
                                    const timeText = time.getAttribute('datetime') || time.textContent.trim();
                                    if (!timeText || !timeText.includes(':')) continue;

                                    const lang = detectLang(row);
                                    const a = time.closest('a') || row.querySelector('a');
                                    out.push({ time: timeText, lang, href: a ? a.href : '' });
                                }
                            }
                        }

                        // Fallback: old .horarioExp structure
                        if (!out.length) {
                            for (const vb of el.querySelectorAll('.horarioExp')) {
                                const lang = detectLang(vb);
                                for (const time of vb.querySelectorAll('time')) {
                                    const timeText = time.getAttribute('datetime') || time.textContent.trim();
                                    if (!timeText || !timeText.includes(':')) continue;
                                    const a = time.closest('a') || vb.querySelector('a');
                                    out.push({ time: timeText, lang, href: a ? a.href : '' });
                                }
                            }
                        }

                        return out;
                    }""")

                    for s in sessions:
                        time_text = (s.get("time") or "")[:5]
                        if not time_text or ":" not in time_text:
                            continue
                        href = s.get("href") or url
                        results.append({
                            "title":    title,
                            "language": s.get("lang") or "es",
                            "date":     date_str,
                            "time":     time_text,
                            "url":      href if href.startswith("http") else BASE_URL + href,
                        })

            except Exception as e:
                print(f"  ⚠ Yelmo error on {date_str}: {e}")
                continue

        browser.close()

    return results
