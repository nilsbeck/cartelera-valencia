# Use Microsoft's official Playwright image so Chromium and all its OS
# dependencies are already installed and version-matched to the playwright
# Python package. Pinning the tag means the image is reproducible.
FROM mcr.microsoft.com/playwright/python:v1.59.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# git is present in the Playwright image, but we re-assert in case the
# upstream ever slims down. tini gives us a proper PID 1 for the
# short-lived container.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps separately so layer cache survives source edits.
COPY scraper/requirements.txt /app/scraper/requirements.txt
RUN pip install -r scraper/requirements.txt

# Copy the rest of the scraper + the run script. The repo itself is cloned
# fresh at runtime so we always operate on the latest main and never push
# stale commits.
COPY scraper /app/scraper
COPY bin     /app/bin

ENTRYPOINT ["/usr/bin/tini", "--", "/app/bin/scrape-and-push.sh"]
