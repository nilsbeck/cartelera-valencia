#!/usr/bin/env bash
# Clone the repo fresh, run the scraper, commit any data/poster changes,
# push to origin/main. Designed to be invoked once per cron tick from a
# host running this image — see DOCKER.md.
#
# Required env:
#   TMDB_API_KEY  — TMDB v3 API key
#   GH_TOKEN      — fine-grained PAT or classic token with `contents: write`
#                   on this single repo
# Optional env:
#   REPO_URL      — defaults to nilsbeck/cartelera-valencia on github.com
#   GIT_USER_NAME / GIT_USER_EMAIL — committer identity
set -euo pipefail

: "${TMDB_API_KEY:?TMDB_API_KEY is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"

REPO_URL="${REPO_URL:-https://github.com/nilsbeck/cartelera-valencia.git}"
GIT_USER_NAME="${GIT_USER_NAME:-cartelera-scraper}"
GIT_USER_EMAIL="${GIT_USER_EMAIL:-scraper@$(hostname)}"
WORK="/work"

# Strip any scheme prefix the user supplied and build the authenticated URL
# we actually push with. Token never appears in logs.
AUTH_URL="https://x-access-token:${GH_TOKEN}@${REPO_URL#https://}"

echo "── cartelera-scraper @ $(date -Iseconds) ──"

rm -rf "$WORK"
git clone --depth=1 --branch=main "$AUTH_URL" "$WORK"
cd "$WORK"
git config user.name  "$GIT_USER_NAME"
git config user.email "$GIT_USER_EMAIL"

# The container ships /app/scraper with its dependencies already installed,
# but the running code should come from the freshly-cloned repo so a `git
# push` of a scraper change is picked up on the next run.
python "$WORK/scraper/run.py" && SCRAPER_EXIT=0 || SCRAPER_EXIT=$?
# Exit codes from run.py:
#   0 = clean, 1 = warnings (data still valid), ≥2 = fatal (skip commit).
if [ "${SCRAPER_EXIT:-0}" -ge 2 ]; then
    echo "scraper fatal (exit $SCRAPER_EXIT) — skipping commit and push"
    exit "$SCRAPER_EXIT"
fi

if git diff --quiet -- data posters; then
    echo "no data changes — nothing to commit"
    exit "$SCRAPER_EXIT"
fi

git add data posters
git commit -m "chore: update showtimes [skip ci]"

# Retry push a few times in case main moved while we were scraping.
for attempt in 1 2 3; do
    if git pull --rebase origin main && git push origin main; then
        echo "pushed (attempt $attempt)"
        # Preserve the warning signal — exit 1 if the scrape had warnings
        # even though the push itself succeeded. Systemd / cron see the
        # unit as failed, which is what you want for "look at the logs".
        exit "${SCRAPER_EXIT:-0}"
    fi
    echo "push attempt $attempt failed, retrying…"
    sleep $((attempt * 5))
done

echo "push failed after 3 attempts" >&2
exit 3
