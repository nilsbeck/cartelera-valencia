"""Tests for the scraper health report (run.py) and its checker (healthcheck.py)."""

from datetime import date, datetime, timedelta, timezone

from run import build_health_report, expected_dates_by_cinema
from healthcheck import evaluate, cinema_problem, summary_markdown

TODAY = date(2026, 11, 10)
NOW = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)


def _rows(cinema, days):
    return [{"cinema": cinema, "date": (TODAY + timedelta(days=d)).isoformat(),
             "time": "20:00", "language": "ES", "title": "X"} for d in days]


class TestBuildHealthReport:
    def test_covers_every_expected_cinema(self):
        report = build_health_report([], today=TODAY)
        assert set(report["cinemas"]) == set(expected_dates_by_cinema(TODAY))
        assert report["date"] == "2026-11-10"
        assert datetime.fromisoformat(report["generated_at"]).tzinfo is not None

    def test_counts_rows_and_days(self):
        report = build_health_report(_rows("babel", [0, 0, 1, 2]), today=TODAY)
        babel = report["cinemas"]["babel"]
        assert babel["rows"] == 4
        assert babel["expected_days"] == 7
        assert babel["covered_days"] == 3
        assert "2026-11-13" in babel["missing_days"]

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
        assert cinema_problem({"rows": 50, "expected_days": 7, "covered_days": 7}) is None

    def test_one_missing_day_is_ok(self):
        assert cinema_problem({"rows": 50, "expected_days": 7, "covered_days": 6}) is None

    def test_error(self):
        assert "raised" in cinema_problem({"rows": 0, "error": "boom"})

    def test_zero_rows(self):
        assert "0 showtimes" in cinema_problem({"rows": 0, "expected_days": 7, "covered_days": 0})

    def test_low_coverage(self):
        assert "2/7" in cinema_problem({"rows": 9, "expected_days": 7, "covered_days": 2})


class TestEvaluate:
    def _report(self, generated=NOW, **cinemas):
        return {"generated_at": generated.isoformat(), "cinemas": cinemas}

    def test_missing(self):
        assert evaluate(None, NOW)["status"] == "missing"
        assert evaluate({}, NOW)["status"] == "missing"

    def test_stale(self):
        old = self._report(NOW - timedelta(hours=40),
                           babel={"rows": 5, "expected_days": 7, "covered_days": 7})
        assert evaluate(old, NOW)["status"] == "stale"

    def test_bad_timestamp_is_stale(self):
        assert evaluate({"generated_at": "nope", "cinemas": {}}, NOW)["status"] == "stale"

    def test_ok(self):
        v = evaluate(self._report(babel={"rows": 5, "expected_days": 7, "covered_days": 7}), NOW)
        assert v == {"status": "ok", "broken": {}, "age_hours": 0.0}

    def test_broken_lists_only_bad_cinemas(self):
        v = evaluate(self._report(
            babel={"rows": 5, "expected_days": 7, "covered_days": 7},
            abc_park={"rows": 0, "expected_days": 7, "covered_days": 0},
        ), NOW)
        assert v["status"] == "broken"
        assert list(v["broken"]) == ["abc_park"]

    def test_real_report_roundtrip(self):
        rows = _rows("babel", range(7))
        report = build_health_report(rows, today=date.today())
        v = evaluate(report)
        assert v["status"] == "broken"            # every other cinema has 0 rows
        assert "babel" not in v["broken"]
        md = summary_markdown(report, v)
        assert "| babel |" in md and "returned 0 showtimes" in md
