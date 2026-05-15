# cartelera-valencia-proxy

Tiny EU-egress HTTP fetch-proxy for cinema scrapers hitting geofenced sites
(currently just Ocine Premium Aqua, which doesn't respond to the GitHub
Actions runner's US IP).

Deployed on Render's free tier in Frankfurt.

## Deploy

1. Sign in to <https://render.com> (free, no credit card).
2. **New +** → **Blueprint** → connect this GitHub repository.
3. Render will read `proxy/render.yaml` and create:
   - a free **web service** in `frankfurt`,
   - a `PROXY_TOKEN` env var with an auto-generated value.
4. After deploy, copy:
   - the service URL (e.g. `https://cartelera-valencia-proxy.onrender.com`),
   - the `PROXY_TOKEN` value (Render → service → Environment → reveal).

## Wire into the scraper

In the GitHub repo settings → **Secrets and variables → Actions**, add:

| Name                | Value                                                  |
| ------------------- | ------------------------------------------------------ |
| `OCINE_PROXY_URL`   | the Render service URL                                 |
| `OCINE_PROXY_TOKEN` | the `PROXY_TOKEN` value                                |

The workflow already forwards these into `python scraper/run.py`; when
`OCINE_PROXY_URL` is set, `scraper/ocine.py` routes its fetches through the
proxy. When it's unset (e.g. local development), the scraper falls back to
direct fetches, which will still fail with `status=None` until you set
it — that's expected.

## Free-tier cold start

The service spins down after 15 minutes of inactivity. The scraper runs
once a day, so every run cold-starts the proxy (~10–30 s on first request,
warm thereafter for all subsequent fetches that day). `ocine.py` uses a 60s
timeout on the first call to absorb this.

## Adding more hosts

Edit `ALLOWED_HOSTS` in `render.yaml` (or in Render's Environment UI for a
no-redeploy change), comma-separated, no scheme:

```
ALLOWED_HOSTS=ocinepremiumaqua.es,www.ocinepremiumaqua.es,another-cinema.es
```

The proxy refuses requests for any host not in the list, so it can't be
turned into an open relay.
