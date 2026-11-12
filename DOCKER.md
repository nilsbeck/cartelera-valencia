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

## Scheduling

### Cron

Add to your user crontab (`crontab -e`) — pick an hour appropriate for
your timezone:

```
30 5 * * *  cd /path/to/cartelera-valencia && /usr/bin/docker compose run --rm scraper >> /var/log/cartelera.log 2>&1
```

That's it. The compose file pulls `TMDB_API_KEY` and `GH_TOKEN` from the
gitignored `.env` automatically.

### systemd timer (Arch and other cron-less distros)

Arch doesn't ship `cron` by default. Use the unit files in
[`systemd/`](systemd/) instead:

```
sudo cp systemd/cartelera-scraper.service systemd/cartelera-scraper.timer /etc/systemd/system/
sudo $EDITOR /etc/systemd/system/cartelera-scraper.service   # set WorkingDirectory
sudo systemctl daemon-reload
sudo systemctl enable --now cartelera-scraper.timer
```

Check it's actually scheduled and see run history:

```
systemctl list-timers cartelera-scraper.timer   # shows next/last fire time
journalctl -u cartelera-scraper.service --since "7 days ago"
```

Run it once immediately, without waiting for the timer:

```
sudo systemctl start cartelera-scraper.service
journalctl -u cartelera-scraper.service -f
```

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

## Automatic scraper fixes (Scraper health workflow)

Every run writes `data/health.json`: per cinema, how many showtimes the
scraper returned, showtimes per day for the coming week, any error, and the
commit the scrapers ran from. The Docker job commits it with the rest of
`data/`, so it reflects what the scrapers get from your home IP.

`.github/workflows/scraper-health.yml` runs daily at 09:23 UTC and judges
that file with `scraper/healthcheck.py`:

| Verdict  | Meaning | What happens |
|----------|---------|--------------|
| `ok`     | every cinema looks fine | nothing |
| `broken` | a scraper raised, returned 0 showtimes, or has none for today or tomorrow | Claude (via `anthropics/claude-code-action`) reproduces the failure on the runner, fixes the scraper, runs the tests and opens a PR labelled `scraper-autofix`, assigned to you. If it can't reach the site (Ocine blocks GitHub IPs) or isn't confident, it opens an issue instead. |
| `stale`  | the report is more than 36 h old | an issue labelled `scraper-health`: the Docker job stopped pushing (not running, or failing before the commit) — not a code problem |
| `missing` | no report yet (before the first Docker run with this code) | nothing |
| `pending` | scraper code on `main` changed after the report was produced | nothing; the next Docker run reports on the new code |

"Broken" deliberately does not require a full week of showtimes: cinemas
publish the next week mid-week (on a Tuesday ABC only lists through
Thursday; on a Thursday Lys has nothing for Friday yet).

While a `scraper-autofix` PR or issue is open, no new fix is started, so
you get at most one at a time. Merge or close it to re-arm. To silence a
cinema that is legitimately closed, set the repository variable
`SCRAPER_HEALTH_IGNORE` (Settings → Secrets and variables → Actions →
Variables) to its id, e.g. `dor`; separate several with spaces.

One-time setup on GitHub:

1. Add a repository secret (Settings → Secrets and variables → Actions):
   either `CLAUDE_CODE_OAUTH_TOKEN` (Claude Pro/Max — create it with
   `claude setup-token`) or `ANTHROPIC_API_KEY`.
2. Settings → Actions → General → Workflow permissions → tick
   **Allow GitHub Actions to create and approve pull requests**.

To try the fix job without waiting for a real breakage, run the workflow
manually (Actions → Scraper health → Run workflow) and put a cinema id
such as `abc_park` in **force_cinemas**. As that scraper works, Claude
should open an issue saying so rather than change code.

PRs opened by the workflow use the built-in `GITHUB_TOKEN`, so GitHub does
not start the `Tests` workflow on them automatically (Claude runs the
tests itself and reports the result in the PR). Push any commit to the
branch to run CI on it.
