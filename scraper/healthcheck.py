#!/usr/bin/env python3
"""
Judge data/health.json (written by run.py on the Docker host) and report
which scrapers look broken. Used by .github/workflows/scraper-health.yml.

Run:  python scraper/healthcheck.py [--health data/health.json] [--github-output]

Exit code is always 0 — the verdict goes to stdout and, with
--github-output, to $GITHUB_OUTPUT as:
  status   ok | broken | stale | missing
  broken   space-separated cinema ids (empty unless status=broken)
  summary  path to a Markdown summary of the report
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent

# The Docker job runs once a day; allow for a late or retried run before
# calling the report stale.
MAX_AGE_HOURS = 36

# A cinema with showtimes on fewer than this share of its expected days is
# treated as broken (one missing day can be a legitimate closure).
MIN_COVERAGE = 0.5


def cinema_problem(info: dict) -> "str | None":
    """Why this cinema counts as broken, or None if it looks healthy."""
    if info.get("error"):
        return f"scraper raised: {info['error']}"
    if not info.get("rows"):
        return "returned 0 showtimes"
    expected = info.get("expected_days") or 0
    covered = info.get("covered_days") or 0
    if expected and covered / expected < MIN_COVERAGE:
        return f"showtimes on only {covered}/{expected} expected days"
    return None


def evaluate(report: "dict | None", now: "datetime | None" = None) -> dict:
    """Return {"status", "broken": {cid: reason}, "age_hours"}."""
    now = now or datetime.now(timezone.utc)
    if not report or "cinemas" not in report:
        return {"status": "missing", "broken": {}, "age_hours": None}

    try:
        generated = datetime.fromisoformat(report["generated_at"])
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)
        age_hours = (now - generated).total_seconds() / 3600
    except (KeyError, TypeError, ValueError):
        age_hours = None

    if age_hours is None or age_hours > MAX_AGE_HOURS:
        return {"status": "stale", "broken": {}, "age_hours": age_hours}

    broken = {
        cid: problem
        for cid, info in sorted(report["cinemas"].items())
        if (problem := cinema_problem(info))
    }
    return {
        "status": "broken" if broken else "ok",
        "broken": broken,
        "age_hours": age_hours,
    }


def summary_markdown(report: "dict | None", verdict: dict) -> str:
    lines = [f"**Status:** `{verdict['status']}`"]
    if verdict["age_hours"] is not None:
        lines.append(f"**Report age:** {verdict['age_hours']:.1f} h "
                     f"(generated {report.get('generated_at')})")
    if verdict["status"] == "missing":
        lines.append("\n`data/health.json` is missing — the Docker job has not "
                     "written a report yet.")
    elif verdict["status"] == "stale":
        lines.append(f"\nThe report is older than {MAX_AGE_HOURS} h, so the Docker "
                     "scrape job on the home server has probably stopped running. "
                     "This is not a scraper bug.")
    if report and "cinemas" in report:
        lines += ["", "| Cinema | Rows | Days covered | Missing days | Problem |",
                  "|---|---|---|---|---|"]
        for cid, info in sorted(report["cinemas"].items()):
            problem = verdict["broken"].get(cid, "")
            missing = ", ".join(info.get("missing_days") or []) or "–"
            lines.append(
                f"| {cid} | {info.get('rows', 0)} | "
                f"{info.get('covered_days', 0)}/{info.get('expected_days', 0)} | "
                f"{missing} | {problem or 'ok'} |"
            )
        diag_lines = [
            f"- **{cid}**: " + "; ".join(info["diagnostics"])
            for cid, info in sorted(report["cinemas"].items())
            if cid in verdict["broken"] and info.get("diagnostics")
        ]
        if diag_lines:
            lines += ["", "Scraper diagnostics:", *diag_lines]
    return "\n".join(lines) + "\n"


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--health", default=str(ROOT / "data" / "health.json"))
    ap.add_argument("--summary-file", default="health-summary.md")
    ap.add_argument("--github-output", action="store_true")
    args = ap.parse_args(argv)

    try:
        report = json.loads(Path(args.health).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = None

    verdict = evaluate(report)
    summary = summary_markdown(report, verdict)
    Path(args.summary_file).write_text(summary, encoding="utf-8")
    print(summary)

    if args.github_output and os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"status={verdict['status']}\n")
            f.write(f"broken={' '.join(verdict['broken'])}\n")
            f.write(f"summary={args.summary_file}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
