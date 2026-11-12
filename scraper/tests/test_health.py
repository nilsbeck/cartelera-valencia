"""Tests for the scraper health report (run.py) and its checker (healthcheck.py)."""

from datetime import date, datetime, timedelta, timezone

from run import build_health_report, current_code_sha, expected_dates_by_cinema
from healthcheck import cinema_problem, evaluate, main, summary_markdown

TODAY = date(2026, 11, 10)   # a Tuesday
NOW = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)


def _rows(cinema, offsets, today=TODAY):
    return [{"cinema": cinema, "date": (today + timedelta(days=d)).isoformat(),
             "time": "20:00", "language": "ES", "title": "X"} for d in offsets]


def _info(counts, start=TODAY, **extra):
    """Health entry with per-day counts for start, start+1, …"""
    days = {(start + timedelta(days=i)).isoformat(): n for i, n in enumerate(counts)}
    info = {"rows": sum(counts), "days": days, "expected_days": 7,
            "covered_days": sum(1 for n in counts if n), "error": None}
    info.update(extra)
    return info


def _report(cinemas, generated=NOW, day=TODAY):
    return {"generated_at": generated.isoformat(), "date": day.isoformat(),
            "cinemas": cinemas}


# Per-day showtime counts (today … today+6) from the real data/showtimes.json
# snapshots. Both are healthy weeks and must not be reported as broken.
TUESDAY_2026_11_10 = {   # most cinemas had only published through Thursday
    "abc_elsaler":   [34, 34, 34, 0, 0, 0, 0],
    "abc_granturia": [42, 42, 42, 0, 0, 0, 0],
    "abc_park":      [41, 42, 40, 5, 2, 0, 0],
    "babel":         [13, 4, 19, 9, 3, 2, 1],
    "dor":           [4, 4, 4, 4, 4, 4, 4],
    "kinepolis":     [31, 29, 29, 3, 7, 6, 0],
    "lys":           [35, 50, 35, 0, 0, 1, 0],
    "mn4":           [44, 44, 44, 34, 34, 30, 20],
    "ocine":         [36, 35, 35, 0, 0, 0, 0],
    "yelmo":         [48, 48, 46, 6, 8, 7, 0],
}
THURSDAY_2026_11_12 = {  # Lys had not published Friday's programme yet
    "abc_elsaler":   [34, 33, 33, 42, 32, 32, 33],
    "abc_granturia": [42, 41, 41, 41, 41, 41, 41],
    "abc_park":      [46, 43, 45, 58, 44, 47, 44],
    "babel":         [19, 9, 4, 2, 2, 1, 0],
    "dor":           [4, 4, 4, 4, 4, 4, 4],
    "kinepolis":     [29, 46, 59, 59, 29, 29, 28],
    "lys":           [36, 0, 0, 2, 0, 0, 0],
    "mn4":           [44, 32, 32, 28, 19, 19, 17],
    "ocine":         [35, 38, 48, 46, 36, 36, 36],
    "yelmo":         [50, 40, 40, 42, 41, 40, 34],
}


class TestBuildHealthReport:
    def test_covers_every_expected_cinema(self):
        report = build_health_report([], today=TODAY)
        assert set(report["cinemas"]) == set(expected_dates_by_cinema(TODAY))
        assert report["date"] == "2026-11-10"
        assert datetime.fromisoformat(report["generated_at"]).tzinfo is not None

    def test_counts_rows_and_days(self):
        report = build_health_report(_rows("babel", [0, 0, 1, 2, 9]), today=TODAY)
        babel = report["cinemas"]["babel"]
        assert babel["rows"] == 5
        assert list(babel["days"]) == [(TODAY + timedelta(days=i)).isoformat() for i in range(7)]
        assert babel["days"]["2026-11-10"] == 2
        assert babel["days"]["2026-11-13"] == 0
        assert babel["expected_days"] == 7
        assert babel["covered_days"] == 3
        assert "2026-11-13" in babel["missing_days"]

    def test_ocine_window_follows_given_day(self):
        # Tuesday → through Thursday: 3 days
        assert build_health_report([], today=TODAY)["cinemas"]["ocine"]["expected_days"] == 3

    def test_records_code_sha(self):
        assert build_health_report([], today=TODAY)["code_sha"] is None
        sha = "a" * 40
        assert build_health_report([], today=TODAY, code_sha=sha)["code_sha"] == sha

    def test_current_code_sha_in_this_checkout(self):
        sha = current_code_sha()
        assert sha is None or (len(sha) == 40 and all(c in "0123456789abcdef" for c in sha))

    def test_records_errors_and_diagnostics(self):
        report = build_health_report(
            [], [("kinepolis", RuntimeError("403"))],
            {"kinepolis": ["blocked: 'Access denied'"]}, today=TODAY,
        )
        k = report["cinemas"]["kinepolis"]
        assert k["error"] == "403"
        assert k["diagnostics"] == ["blocked: 'Access denied'"]
        assert report["cinemas"]["babel"]["error"] is None


class TestCinemaProblem:
    def test_healthy(self):
        assert cinema_problem(_info([30] * 7), "2026-11-10") is None

    def test_only_published_through_thursday_is_ok(self):
        assert cinema_problem(_info([34, 34, 34, 0, 0, 0, 0]), "2026-11-10") is None

    def test_today_only_is_ok(self):
        assert cinema_problem(_info([36, 0, 0, 2, 0, 0, 0]), "2026-11-10") is None

    def test_tomorrow_only_is_ok(self):
        assert cinema_problem(_info([0, 12, 12, 0, 0, 0, 0]), "2026-11-10") is None

    def test_error(self):
        assert "raised: boom" in cinema_problem(_info([0] * 7, error="boom"), "2026-11-10")

    def test_zero_rows(self):
        assert cinema_problem(_info([0] * 7), "2026-11-10") == "returned 0 showtimes"

    def test_nothing_today_or_tomorrow(self):
        problem = cinema_problem(_info([0, 0, 5, 5, 5, 5, 5]), "2026-11-10")
        assert problem == "no showtimes for 2026-11-10 or 2026-11-11"

    def test_falls_back_to_first_day_without_report_date(self):
        assert cinema_problem(_info([0, 0, 5, 0, 0, 0, 0])) is not None

    def test_legacy_entry_without_days(self):
        assert cinema_problem({"rows": 3, "covered_days": 0}) is not None
        assert cinema_problem({"rows": 3, "covered_days": 2}) is None


class TestEvaluate:
    def test_missing(self):
        assert evaluate(None, NOW)["status"] == "missing"
        assert evaluate({}, NOW)["status"] == "missing"

    def test_stale(self):
        old = _report({"babel": _info([5] * 7)}, generated=NOW - timedelta(hours=40))
        assert evaluate(old, NOW)["status"] == "stale"

    def test_bad_timestamp_is_stale(self):
        assert evaluate({"generated_at": "nope", "cinemas": {}}, NOW)["status"] == "stale"

    def test_pending_when_code_changed_after_report(self):
        report = _report({"abc_park": _info([0] * 7)})
        assert evaluate(report, NOW, code_changed=True)["status"] == "pending"
        assert evaluate(report, NOW, code_changed=False)["status"] == "broken"

    def test_stale_wins_over_pending(self):
        old = _report({"abc_park": _info([0] * 7)}, generated=NOW - timedelta(hours=40))
        assert evaluate(old, NOW, code_changed=True)["status"] == "stale"

    def test_ok(self):
        v = evaluate(_report({"babel": _info([5] * 7)}), NOW)
        assert v == {"status": "ok", "broken": {}, "ignored": {}, "age_hours": 0.0}

    def test_broken_lists_only_bad_cinemas(self):
        v = evaluate(_report({"babel": _info([5] * 7), "abc_park": _info([0] * 7)}), NOW)
        assert v["status"] == "broken"
        assert list(v["broken"]) == ["abc_park"]

    def test_ignore(self):
        v = evaluate(_report({"babel": _info([5] * 7), "dor": _info([0] * 7)}), NOW,
                     ignore={"dor"})
        assert v["status"] == "ok"
        assert v["ignored"] == {"dor": "returned 0 showtimes"}

    def test_real_tuesday_snapshot_is_ok(self):
        cinemas = {cid: _info(c) for cid, c in TUESDAY_2026_11_10.items()}
        assert evaluate(_report(cinemas), NOW)["status"] == "ok"

    def test_real_thursday_snapshot_is_ok(self):
        thu = date(2026, 11, 12)
        cinemas = {cid: _info(c, start=thu) for cid, c in THURSDAY_2026_11_12.items()}
        report = _report(cinemas, day=thu)
        assert evaluate(report, NOW)["status"] == "ok"

    def test_abc_breakage_is_detected(self):
        cinemas = {cid: _info(c) for cid, c in TUESDAY_2026_11_10.items()}
        for cid in ("abc_park", "abc_elsaler", "abc_granturia"):
            cinemas[cid] = _info([0] * 7)
        v = evaluate(_report(cinemas), NOW)
        assert v["status"] == "broken"
        assert sorted(v["broken"]) == ["abc_elsaler", "abc_granturia", "abc_park"]


class TestSummaryAndCli:
    def test_real_report_roundtrip(self):
        report = build_health_report(_rows("babel", range(7), date.today()))
        v = evaluate(report)
        assert v["status"] == "broken"            # every other cinema has 0 rows
        assert "babel" not in v["broken"]
        md = summary_markdown(report, v)
        assert "| babel | 7 | 1 | 1 | 7/7 | ok |" in md
        assert "**returned 0 showtimes**" in md

    def test_cli_writes_github_output(self, tmp_path, monkeypatch):
        import json
        health = tmp_path / "health.json"
        report = build_health_report(_rows("babel", range(7), date.today()))
        health.write_text(json.dumps(report))
        out = tmp_path / "out.txt"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        main(["--health", str(health), "--summary-file", str(tmp_path / "s.md"),
              "--ignore", "dor, ocine", "--github-output"])
        lines = out.read_text().splitlines()
        assert lines[0] == "status=broken"
        broken = lines[1].removeprefix("broken=").split()
        assert "babel" not in broken and "dor" not in broken and "ocine" not in broken
        assert "abc_park" in broken

    def test_cli_code_changed_flag(self, tmp_path, monkeypatch):
        import json
        health = tmp_path / "health.json"
        health.write_text(json.dumps(build_health_report([])))
        out = tmp_path / "out.txt"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        main(["--health", str(health), "--summary-file", str(tmp_path / "s.md"),
              "--code-changed", "--github-output"])
        assert out.read_text().splitlines() == ["status=pending", "broken="]

    def test_cli_missing_file(self, tmp_path, monkeypatch):
        out = tmp_path / "out.txt"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        main(["--health", str(tmp_path / "nope.json"),
              "--summary-file", str(tmp_path / "s.md"), "--github-output"])
        assert out.read_text().splitlines()[0] == "status=missing"
