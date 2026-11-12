#!/usr/bin/env python3
"""
Judge data/health.json (written by run.py on the Docker host) and report
which scrapers look broken. Used by .github/workflows/scraper-health.yml.

Run:  python scraper/healthcheck.py [--health data/health.json]
          [--ignore "dor ocine"] [--code-changed] [--github-output]

Exit code is always 0 — the verdict goes to stdout and, with
--github-output, to $GITHUB_OUTPUT as:
  status   ok | broken | stale | missing | pending
  broken   space-separated cinema ids (empty unless status=broken)

Statuses:
  ok       every cinema looks healthy (or is in --ignore)
  broken   at least one scraper raised, returned nothing, or has no
           showtimes for today or tomorrow
  stale    the report is older than MAX_AGE_HOURS: the Docker job stopped
           pushing (not running, or crashing before the commit)
  missing  no report yet (before the first Docker run that writes one)
  pending  scraper code on main changed after the commit the report was
           produced from (--code-changed, worked out by the workflow from
           the report's code_sha), so it says nothing about the current
           code — wait for the next Docker run
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent

# The Docker job runs once a day; allow for a late or retried run before
# calling the report stale.
MAX_AGE_HOURS = 36


def _parse_ts(value: "str | None") -> "datetime | None":
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def cinema_problem(info: dict, report_date: "str | None" = None) -> "str | None":
    """Why this cinema counts as broken, or None if it looks healthy.

    Deliberately NOT a "covers most of the week" rule: cinemas publish the
    next week's programme mid-week (ABC on Tuesday shows only Tue–Thu, Lys
    on Thursday shows nothing for Friday yet), so only today and tomorrow
    are reliably present for a working scraper.
    """
    if info.get("error"):
        return f"scraper raised: {info['error']}"
    if not info.get("rows"):
        return "returned 0 showtimes"
    days = info.get("days")
    if days:
        try:
            first = date.fromisoformat(report_date) if report_date else None
        except ValueError:
            first = None
        if first is None:
            first = date.fromisoformat(sorted(days)[0])
        today, tomorrow = first.isoformat(), (first + timedelta(days=1)).isoformat()
        if not days.get(today) and not days.get(tomorrow):
            return f"no showtimes for {today} or {tomorrow}"
    elif not info.get("covered_days"):  # report written before "days" existed
        return "no showtimes in its expected date window"
    return None


def evaluate(
    report: "dict | None",
    now: "datetime | None" = None,
    ignore: "set[str] | frozenset[str]" = frozenset(),
    code_changed: bool = False,
) -> dict:
    """Return {"status", "broken", "ignored", "age_hours"}.

    "broken" and "ignored" map cinema id → problem description.
    """
    now = now or datetime.now(timezone.utc)
    verdict = {"status": "missing", "broken": {}, "ignored": {}, "age_hours": None}
    if not report or "cinemas" not in report:
        return verdict

    generated = _parse_ts(report.get("generated_at"))
    if generated is not None:
        verdict["age_hours"] = (now - generated).total_seconds() / 3600
    if generated is None or verdict["age_hours"] > MAX_AGE_HOURS:
        verdict["status"] = "stale"
        return verdict
    if code_changed:
        verdict["status"] = "pending"
        return verdict

    for cid, info in sorted(report["cinemas"].items()):
        problem = cinema_problem(info, report.get("date"))
        if problem:
            (verdict["ignored"] if cid in ignore else verdict["broken"])[cid] = problem
    verdict["status"] = "broken" if verdict["broken"] else "ok"
    return verdict


_STATUS_TEXT = {
    "missing": "`data/health.json` does not exist yet: the Docker job has not "
               "run with the health-report code. Nothing to do.",
    "stale":   f"The report is older than {MAX_AGE_HOURS} h, so the Docker job on "
               "the home server has stopped pushing: it is not running, or it "
               "fails before the commit. Check it with "
               "`journalctl -u cartelera-scraper.service` (or the cron log). "
               "This is not a scraper bug.",
    "pending": "Scraper code changed on main after this report was written, so "
               "it does not reflect the current code. Waiting for the next "
               "Docker run.",
}


def summary_markdown(report: "dict | None", verdict: dict) -> str:
    lines = [f"**Status:** `{verdict['status']}`"]
    if verdict["age_hours"] is not None and report:
        lines.append(f"**Report:** generated {report.get('generated_at')} "
                     f"({verdict['age_hours']:.1f} h ago)")
    if verdict["status"] in _STATUS_TEXT:
        lines += ["", _STATUS_TEXT[verdict["status"]]]

    if report and "cinemas" in report:
        lines += ["", "| Cinema | Rows | Today | Tomorrow | Expected days covered | Problem |",
                  "|---|---|---|---|---|---|"]
        day0 = report.get("date") or ""
        try:
            day1 = (date.fromisoformat(day0) + timedelta(days=1)).isoformat()
        except ValueError:
            day1 = ""
        for cid, info in sorted(report["cinemas"].items()):
            days = info.get("days") or {}
            if cid in verdict["broken"]:
                problem = f"**{verdict['broken'][cid]}**"
            elif cid in verdict["ignored"]:
                problem = f"{verdict['ignored'][cid]} (ignored)"
            else:
                problem = "ok"
            lines.append(
                f"| {cid} | {info.get('rows', 0)} | {days.get(day0, '–')} | "
                f"{days.get(day1, '–')} | "
                f"{info.get('covered_days', 0)}/{info.get('expected_days', 0)} | "
                f"{problem} |"
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
    ap = argparse.ArgumentParser(description="Judge data/health.json.")
    ap.add_argument("--health", default=str(ROOT / "data" / "health.json"))
    ap.add_argument("--summary-file", default="health-summary.md")
    ap.add_argument("--ignore", default="",
                    help="cinema ids to never report as broken (space/comma separated)")
    ap.add_argument("--code-changed", action="store_true",
                    help="scraper/ changed on main since the report's code_sha")
    ap.add_argument("--github-output", action="store_true")
    args = ap.parse_args(argv)

    try:
        report = json.loads(Path(args.health).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = None

    ignore = {c for c in args.ignore.replace(",", " ").split() if c}
    verdict = evaluate(report, ignore=ignore, code_changed=args.code_changed)
    summary = summary_markdown(report, verdict)
    Path(args.summary_file).write_text(summary, encoding="utf-8")
    print(summary)

    if args.github_output and os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"status={verdict['status']}\n")
            f.write(f"broken={' '.join(verdict['broken'])}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
