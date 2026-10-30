"""Automated tests for scripts/log_summary.py.

Covers DEV-004.1 (parser), DEV-004.2 (HTTP aggregation),
DEV-004.3 (hourly stats) and CLI behavior via subprocess.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "log_summary.py"
sys.path.insert(0, str(SCRIPTS_DIR))

import log_summary as ls  # noqa: E402  (import tras ajuste de sys.path)


# ---------------------------------------------------------------------------
# Helpers de construcción de líneas (sin I/O)
# ---------------------------------------------------------------------------

def make_line(
    ip: str = "192.168.1.10",
    ts: str = "28/Oct/2026:10:15:00 -0500",
    method: str = "GET",
    path: str = "/x",
    protocol: str = "HTTP/1.1",
    status: str = "200",
    bytes_sent: str = "10",
    referer: str = "-",
    ua: str = "ua",
) -> str:
    return (
        f'{ip} - - [{ts}] "{method} {path} {protocol}" '
        f'{status} {bytes_sent} "{referer}" "{ua}"\n'
    )


class TempLogMixin:
    """Crea un directorio temporal y escribe logs sintéticos."""

    def setUp(self) -> None:  # noqa: N802 (unittest API)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)

    def write_log(self, name: str, lines: list[str]) -> Path:
        p = self.tmpdir / name
        p.write_text("".join(lines), encoding="utf-8")
        return p

    def write_bytes(self, name: str, data: bytes) -> Path:
        p = self.tmpdir / name
        p.write_bytes(data)
        return p


# ---------------------------------------------------------------------------
# RF-01: parser
# ---------------------------------------------------------------------------

class TestParseTimestamp(unittest.TestCase):
    def test_valid_timestamp_with_negative_offset(self):
        dt = ls.parse_timestamp("28/Oct/2026:10:15:00 -0500")
        self.assertIsNotNone(dt)
        assert dt is not None
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.hour, 10)
        self.assertEqual(dt.utcoffset(), timedelta(hours=-5))

    def test_valid_timestamp_with_positive_offset(self):
        dt = ls.parse_timestamp("01/Jan/2026:00:00:00 +0200")
        self.assertIsNotNone(dt)
        assert dt is not None
        self.assertEqual(dt.utcoffset(), timedelta(hours=2))

    def test_invalid_timestamp_returns_none(self):
        self.assertIsNone(ls.parse_timestamp("32/Oct/2026:10:15:00 -0500"))
        self.assertIsNone(ls.parse_timestamp("28/Oct/2026:25:00:00 -0500"))
        self.assertIsNone(ls.parse_timestamp("not-a-timestamp"))

    def test_missing_offset_returns_none(self):
        self.assertIsNone(ls.parse_timestamp("28/Oct/2026:10:15:00"))


class TestParseLine(unittest.TestCase):
    def test_valid_combined_line(self):
        rec = ls.parse_line(make_line())
        self.assertIsNotNone(rec)
        assert rec is not None
        self.assertEqual(rec.remote_addr, "192.168.1.10")
        self.assertEqual(rec.method, "GET")
        self.assertEqual(rec.path, "/x")
        self.assertEqual(rec.protocol, "HTTP/1.1")
        self.assertEqual(rec.status, 200)
        self.assertEqual(rec.bytes_sent, 10)

    def test_accepts_non_get_methods(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "PROPFIND"):
            with self.subTest(method=method):
                rec = ls.parse_line(make_line(method=method))
                self.assertIsNotNone(rec)
                assert rec is not None
                self.assertEqual(rec.method, method)

    def test_invalid_timestamp_yields_none(self):
        self.assertIsNone(ls.parse_line(make_line(ts="32/Oct/2026:10:15:00 -0500")))

    def test_status_out_of_range_yields_none(self):
        self.assertIsNone(ls.parse_line(make_line(status="099")))
        self.assertIsNone(ls.parse_line(make_line(status="600")))

    def test_non_numeric_bytes_yields_none(self):
        self.assertIsNone(ls.parse_line(make_line(bytes_sent="abc")))

    def test_dash_bytes_yields_none(self):
        self.assertIsNone(ls.parse_line(make_line(bytes_sent="-")))

    def test_malformed_lines_yield_none(self):
        for bad in (
            "",
            "\n",
            "totally not a log line\n",
            '192.168.1.10 - - [28/Oct/2026:10:15:00 -0500] "GET /x"\n',
        ):
            with self.subTest(line=bad):
                self.assertIsNone(ls.parse_line(bad))


# ---------------------------------------------------------------------------
# RF-02: agregación HTTP
# ---------------------------------------------------------------------------

class TestClassifyStatus(unittest.TestCase):
    def test_non_errors(self):
        for s in (100, 200, 204, 301, 304, 399):
            with self.subTest(status=s):
                self.assertEqual(ls.classify_status(s), "non_error")

    def test_client_errors(self):
        for s in (400, 403, 404, 451, 499):
            with self.subTest(status=s):
                self.assertEqual(ls.classify_status(s), "client_error")

    def test_server_errors(self):
        for s in (500, 502, 503, 599):
            with self.subTest(status=s):
                self.assertEqual(ls.classify_status(s), "server_error")


class TestAggregation(TempLogMixin, unittest.TestCase):
    def _agg(self, lines: list[str]) -> ls.Aggregation:
        return ls.aggregate(self.write_log("x.log", lines))

    def test_no_http_errors(self):
        agg = self._agg([
            make_line(status="200"),
            make_line(status="301"),
            make_line(status="204"),
        ])
        self.assertEqual(agg.valid_records, 3)
        self.assertEqual(agg.invalid_records, 0)
        self.assertEqual(agg.non_error, 3)
        self.assertEqual(agg.client_errors, 0)
        self.assertEqual(agg.server_errors, 0)
        self.assertEqual(agg.total_http_errors, 0)
        self.assertEqual(dict(agg.error_codes), {})
        self.assertEqual(dict(agg.hourly), {})

    def test_only_4xx(self):
        agg = self._agg([
            make_line(status="404"),
            make_line(status="404"),
            make_line(status="403"),
        ])
        self.assertEqual(agg.client_errors, 3)
        self.assertEqual(agg.server_errors, 0)
        self.assertEqual(agg.error_codes[404], 2)
        self.assertEqual(agg.error_codes[403], 1)

    def test_only_5xx(self):
        agg = self._agg([make_line(status="500"), make_line(status="502")])
        self.assertEqual(agg.client_errors, 0)
        self.assertEqual(agg.server_errors, 2)
        self.assertEqual(agg.error_codes[500], 1)
        self.assertEqual(agg.error_codes[502], 1)

    def test_mixed_4xx_5xx_and_non_error(self):
        agg = self._agg([
            make_line(status="200"),
            make_line(status="404"),
            make_line(status="500"),
        ])
        self.assertEqual(agg.valid_records, 3)
        self.assertEqual(agg.non_error, 1)
        self.assertEqual(agg.client_errors, 1)
        self.assertEqual(agg.server_errors, 1)
        self.assertEqual(agg.total_http_errors, 2)

    def test_invalid_lines_do_not_affect_http_counts(self):
        agg = self._agg([
            "garbage\n",
            make_line(status="404"),
            make_line(ts="32/Oct/2026:10:15:00 -0500", status="500"),
        ])
        self.assertEqual(agg.total_lines, 3)
        self.assertEqual(agg.valid_records, 1)
        self.assertEqual(agg.invalid_records, 2)
        self.assertEqual(agg.client_errors, 1)
        self.assertEqual(agg.server_errors, 0)
        self.assertEqual(agg.error_codes[500], 0)

    def test_valid_records_equal_sum_of_categories(self):
        agg = self._agg([
            make_line(status="200"),
            make_line(status="301"),
            make_line(status="404"),
            make_line(status="500"),
        ])
        self.assertEqual(
            agg.non_error + agg.client_errors + agg.server_errors,
            agg.valid_records,
        )

    def test_total_http_errors_is_sum_of_categories(self):
        agg = self._agg([
            make_line(status="403"),
            make_line(status="500"),
            make_line(status="502"),
        ])
        self.assertEqual(
            agg.total_http_errors,
            agg.client_errors + agg.server_errors,
        )


# ---------------------------------------------------------------------------
# RF-03: estadísticas horarias
# ---------------------------------------------------------------------------

class TestHourly(TempLogMixin, unittest.TestCase):
    def _agg(self, lines: list[str]) -> ls.Aggregation:
        return ls.aggregate(self.write_log("h.log", lines))

    def _hour_of(self, ts: str) -> datetime:
        dt = ls.parse_timestamp(ts)
        assert dt is not None
        return ls.hour_key(dt)

    def test_same_hour_aggregated(self):
        agg = self._agg([
            make_line(ts="28/Oct/2026:10:00:01 -0500", status="404"),
            make_line(ts="28/Oct/2026:10:15:00 -0500", status="403"),
            make_line(ts="28/Oct/2026:10:59:59 -0500", status="500"),
        ])
        self.assertEqual(len(agg.hourly), 1)
        bucket = next(iter(agg.hourly.values()))
        self.assertEqual(bucket.client_errors, 2)
        self.assertEqual(bucket.server_errors, 1)
        self.assertEqual(bucket.total, 3)

    def test_different_hours_separate_buckets(self):
        agg = self._agg([
            make_line(ts="28/Oct/2026:08:10:00 -0500", status="404"),
            make_line(ts="28/Oct/2026:09:10:00 -0500", status="404"),
            make_line(ts="28/Oct/2026:10:10:00 -0500", status="404"),
        ])
        self.assertEqual(len(agg.hourly), 3)

    def test_same_hour_different_dates_separate(self):
        agg = self._agg([
            make_line(ts="28/Oct/2026:10:10:00 -0500", status="404"),
            make_line(ts="29/Oct/2026:10:10:00 -0500", status="404"),
        ])
        self.assertEqual(len(agg.hourly), 2)

    def test_different_offsets_same_clock_are_distinct(self):
        agg = self._agg([
            make_line(ts="28/Oct/2026:10:10:00 -0500", status="404"),
            make_line(ts="28/Oct/2026:10:10:00 +0000", status="404"),
        ])
        self.assertEqual(len(agg.hourly), 2)
        offsets = {dt.utcoffset() for dt in agg.hourly}
        self.assertEqual(offsets, {timedelta(hours=-5), timedelta(0)})

    def test_hourly_totals_match_global_totals(self):
        agg = self._agg([
            make_line(ts="28/Oct/2026:08:10:00 -0500", status="404"),
            make_line(ts="28/Oct/2026:09:10:00 -0500", status="404"),
            make_line(ts="28/Oct/2026:09:20:00 -0500", status="500"),
            make_line(ts="28/Oct/2026:10:10:00 -0500", status="502"),
        ])
        sum_client = sum(b.client_errors for b in agg.hourly.values())
        sum_server = sum(b.server_errors for b in agg.hourly.values())
        sum_total = sum(b.total for b in agg.hourly.values())
        self.assertEqual(sum_client, agg.client_errors)
        self.assertEqual(sum_server, agg.server_errors)
        self.assertEqual(sum_total, agg.total_http_errors)

    def test_sort_key_orders_by_absolute_instant(self):
        # 10:00 -0500 == 15:00 UTC; 10:00 +0000 == 10:00 UTC
        # El de +0000 debe ir PRIMERO en orden cronológico absoluto.
        dt_neg = ls.hour_key(ls.parse_timestamp("28/Oct/2026:10:00:00 -0500"))  # type: ignore[arg-type]
        dt_utc = ls.hour_key(ls.parse_timestamp("28/Oct/2026:10:00:00 +0000"))  # type: ignore[arg-type]
        ordered = sorted(
            [(dt_neg, ls.HourBucket()), (dt_utc, ls.HourBucket())],
            key=ls.sort_key,
        )
        self.assertEqual(ordered[0][0], dt_utc)

    def test_peak_tie_resolves_to_earliest_in_utc(self):
        # Dos intervalos con 1 error cada uno; el de 10:00 +0000 (10:00 UTC)
        # es anterior en absoluto al de 10:00 -0500 (15:00 UTC).
        lines = [
            make_line(ts="28/Oct/2026:10:00:00 -0500", status="404"),
            make_line(ts="28/Oct/2026:10:00:00 +0000", status="404"),
        ]
        agg = ls.aggregate(self.write_log("h.log", lines))
        ordered = sorted(agg.hourly.items(), key=ls.sort_key)
        peak_dt, peak_bucket = max(ordered, key=lambda kv: kv[1].total)
        self.assertEqual(peak_bucket.total, 1)
        self.assertEqual(peak_dt.utcoffset(), timedelta(0))


# ---------------------------------------------------------------------------
# Helpers de presentación
# ---------------------------------------------------------------------------

class TestFormatHelpers(unittest.TestCase):
    def test_format_reason_known(self):
        self.assertEqual(ls.format_reason(404), "404 Not Found")
        self.assertEqual(ls.format_reason(500), "500 Internal Server Error")

    def test_format_reason_unknown(self):
        self.assertEqual(ls.format_reason(499), "HTTP 499 (unknown reason)")

    def test_format_hour_label(self):
        dt = ls.parse_timestamp("28/Oct/2026:10:00:00 -0500")
        assert dt is not None
        label = ls.format_hour_label(ls.hour_key(dt))
        self.assertEqual(label, "2026-10-28 10:00 -0500")

    def test_hour_key_truncates_minutes_and_keeps_tzinfo(self):
        dt = ls.parse_timestamp("28/Oct/2026:10:45:33 -0500")
        assert dt is not None
        key = ls.hour_key(dt)
        self.assertEqual(key.minute, 0)
        self.assertEqual(key.second, 0)
        self.assertEqual(key.utcoffset(), timedelta(hours=-5))


# ---------------------------------------------------------------------------
# RF-04: CLI vía subprocess
# ---------------------------------------------------------------------------

class TestCLI(TempLogMixin, unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *args],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            check=False,
        )

    def test_file_with_errors_exit_0(self):
        p = self.write_log("ok.log", [
            make_line(status="200"),
            make_line(status="404"),
            make_line(status="500"),
        ])
        cp = self._run("--file", str(p))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("NGINX LOG ANALYZER", cp.stdout)
        self.assertIn("PARSING SUMMARY", cp.stdout)
        self.assertIn("HTTP ERROR SUMMARY", cp.stdout)
        self.assertIn("HTTP ERROR DISTRIBUTION", cp.stdout)
        self.assertIn("HOURLY HTTP ERROR STATISTICS", cp.stdout)
        self.assertIn("Analysis completed.", cp.stdout)

    def test_missing_file_argument_exit_2(self):
        cp = self._run()
        self.assertEqual(cp.returncode, 2)
        self.assertIn("--file", cp.stderr)

    def test_nonexistent_file_exit_1(self):
        missing = self.tmpdir / "nope.log"
        cp = self._run("--file", str(missing))
        self.assertEqual(cp.returncode, 1)
        self.assertIn("no existe", cp.stderr)
        self.assertEqual(cp.stdout, "")

    def test_empty_file_exit_0_with_zero_totals(self):
        p = self.write_log("empty.log", [])
        cp = self._run("--file", str(p))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("Total lines:          0", cp.stdout)
        self.assertIn("Valid records:        0", cp.stdout)
        self.assertIn("Invalid records:      0", cp.stdout)
        self.assertIn("No HTTP errors were found.", cp.stdout)

    def test_invalid_utf8_exit_1(self):
        p = self.write_bytes(
            "bad.log",
            b'192.168.1.10 - - [28/Oct/2026:10:00:00 -0500] "GET /\xff HTTP/1.1" 404 10 "-" "ua"\n',
        )
        cp = self._run("--file", str(p))
        self.assertEqual(cp.returncode, 1)
        self.assertIn("UTF-8", cp.stderr)

    def test_headers_present_no_errors(self):
        p = self.write_log("noerr.log", [make_line(status="200")])
        cp = self._run("--file", str(p))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        for header in (
            "NGINX LOG ANALYZER",
            "PARSING SUMMARY",
            "HTTP ERROR SUMMARY",
            "No HTTP errors were found.",
        ):
            with self.subTest(header=header):
                self.assertIn(header, cp.stdout)

    def test_peak_line_present_when_errors_exist(self):
        p = self.write_log("peak.log", [
            make_line(status="404"),
            make_line(status="500"),
        ])
        cp = self._run("--file", str(p))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("Peak error hour:", cp.stdout)
        self.assertIn("Peak error count:", cp.stdout)
        self.assertIn("(ties resolved to the earliest interval)", cp.stdout)


if __name__ == "__main__":
    unittest.main()