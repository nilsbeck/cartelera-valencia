# Running the scraper locally in Docker

The scraper used to run in GitHub Actions, but the GitHub-hosted runner's
US-cloud egress is silently blocked by `ocinepremiumaqua.es` (PR #57).
Running the scrape on your own server in Docker, from your home IP, is
the simplest workaround:

```
┌────────────────┐  cron     ┌────────────────────────┐  git push
│ your server    │ ────────▶ │ cartelera-scraper      │ ─────────▶ origin/main
│ (Linux, docker)│           │ (Playwright + Python)  │
└────────────────┘           └────────────────────────┘            │
                                                                   ▼
                                                          ┌────────────────┐
                                                          │ GitHub Pages   │
                                                          │ (serves        │
                                                          │  index.html +  │
                                                          │  data/*.json)  │
                                                          └────────────────┘
```

## One-time setup

1. Create a fine-grained Personal Access Token on GitHub
   (Settings → Developer settings → Personal access tokens → Fine-grained):
   - Resource owner: yourself
   - Repository access: only `nilsbeck/cartelera-valencia`
   - Permissions → **Contents: Read and write**
   - Copy the token (`github_pat_…`)
2. On your server, clone this repo:
   ```
   git clone https://github.com/nilsbeck/cartelera-valencia.git
   cd cartelera-valencia
   ```
3. Create `.env` next to `docker-compose.yml`:
   ```
   cp .env.example .env
   $EDITOR .env   # paste TMDB_API_KEY and GH_TOKEN
   ```
4. Build the image:
   ```
   docker compose build
   ```

## Running once

```
docker compose run --rm scraper
```

The container clones the repo fresh into `/work`, runs `scraper/run.py`,
commits `data/` and `posters/` if anything changed, and pushes to
`origin/main`. Nothing is persisted on the host filesystem.

## Cron

Add to your user crontab (`crontab -e`) — pick an hour appropriate for
your timezone:

```
30 5 * * *  cd /path/to/cartelera-valencia && /usr/bin/docker compose run --rm scraper >> /var/log/cartelera.log 2>&1
```

That's it. The compose file pulls `TMDB_API_KEY` and `GH_TOKEN` from the
gitignored `.env` automatically.

## Rebuilding

The image bakes in `scraper/requirements.txt` and the scraper source for
its Python dependencies, but the actual script the container runs is the
freshly-cloned `main`. So:

- **Edits to `scraper/*.py`**: just `git push`. The next cron tick clones
  the new code and runs it. No rebuild needed.
- **Edits to `scraper/requirements.txt` or `Dockerfile`**: rebuild with
  `docker compose build --no-cache`.

## Notifications

The scraper can ping you on warnings / failures via
[Apprise](https://github.com/caronc/apprise), which speaks email, Telegram,
Discord, ntfy, Pushover, … 80+ services.

Set `APPRISE_URLS` in `.env` to a whitespace- or comma-separated list of
target URLs. Examples:

```
# Gmail with an app password
APPRISE_URLS=mailto://you:app-password@smtp.gmail.com

# Fastmail / generic SMTPS
APPRISE_URLS=mailtos://you:pass@smtp.fastmail.com:465?from=you@example.com

# Multiple targets
APPRISE_URLS=mailto://… tgram://bottoken/chatid
```

See the [Apprise wiki](https://github.com/caronc/apprise/wiki) for the
exact URL format for each service.

By default you only get pinged when the run finishes with warnings or
crashes. To also get a daily "clean run" heartbeat email, set
`APPRISE_NOTIFY_ON_SUCCESS=1`.

## Disabling the old GitHub Actions schedule

The `Scrape` workflow in `.github/workflows/scrape.yml` now keeps only
its `workflow_dispatch` trigger (no cron) — see the comment block at the
top — so it won't compete with your local cron. You can still trigger it
manually from the Actions tab if you ever want to.
