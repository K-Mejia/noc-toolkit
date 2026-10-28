#!/usr/bin/env python3
"""Nginx access log analyzer (combined format) — NOC Toolkit.

Etapas DEV-004.1 (parser), DEV-004.2 (agregación HTTP) y
DEV-004.3 (estadísticas horarias). Sin gráficos, exportaciones ni CI.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

EXIT_OK = 0
EXIT_READ_ERROR = 1
EXIT_USAGE = 2

_PATTERN = re.compile(
    r'^(?P<remote_addr>\S+)'
    r' - '
    r'(?P<remote_user>\S+)'
    r' \[(?P<time_local>[^\]]+)\]'
    r' "(?P<method>[A-Z]+) (?P<path>\S+) (?P<protocol>HTTP/\d\.\d)"'
    r' (?P<status>\d{3})'
    r' (?P<body_bytes_sent>\d+|-)'
    r' "(?P<referer>[^"]*)"'
    r' "(?P<user_agent>[^"]*)"'
    r'\s*$'
)

_DT_FORMAT = "%d/%b/%Y:%H:%M:%S %z"

_HTTP_REASONS: dict[int, str] = {
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
    404: "Not Found", 405: "Method Not Allowed", 406: "Not Acceptable",
    408: "Request Timeout", 409: "Conflict", 410: "Gone",
    411: "Length Required", 412: "Precondition Failed",
    413: "Payload Too Large", 414: "URI Too Long",
    415: "Unsupported Media Type", 416: "Range Not Satisfiable",
    417: "Expectation Failed", 418: "I'm a teapot",
    421: "Misdirected Request", 422: "Unprocessable Entity",
    425: "Too Early", 426: "Upgrade Required",
    428: "Precondition Required", 429: "Too Many Requests",
    431: "Request Header Fields Too Large",
    451: "Unavailable For Legal Reasons",
    500: "Internal Server Error", 501: "Not Implemented",
    502: "Bad Gateway", 503: "Service Unavailable",
    504: "Gateway Timeout", 505: "HTTP Version Not Supported",
    506: "Variant Also Negotiates", 507: "Insufficient Storage",
    508: "Loop Detected", 510: "Not Extended",
    511: "Network Authentication Required",
}


@dataclass(frozen=True)
class LogRecord:
    remote_addr: str
    timestamp: datetime
    method: str
    path: str
    protocol: str
    status: int
    bytes_sent: int


@dataclass
class HourBucket:
    client_errors: int = 0
    server_errors: int = 0

    @property
    def total(self) -> int:
        return self.client_errors + self.server_errors


@dataclass
class Aggregation:
    total_lines: int = 0
    valid_records: int = 0
    invalid_records: int = 0
    non_error: int = 0
    client_errors: int = 0
    server_errors: int = 0
    error_codes: Counter[int] = field(default_factory=Counter)
    # Clave: datetime truncado a la hora, conservando el offset del registro.
    hourly: dict[datetime, HourBucket] = field(default_factory=dict)

    @property
    def total_http_errors(self) -> int:
        return self.client_errors + self.server_errors


def parse_timestamp(raw: str) -> datetime | None:
    try:
        return datetime.strptime(raw, _DT_FORMAT)
    except ValueError:
        return None


def parse_line(line: str) -> LogRecord | None:
    line = line.rstrip("\n")
    match = _PATTERN.match(line)
    if match is None:
        return None

    ts = parse_timestamp(match.group("time_local"))
    if ts is None:
        return None

    status = int(match.group("status"))
    if not (100 <= status <= 599):
        return None

    raw_bytes = match.group("body_bytes_sent")
    if raw_bytes == "-":
        return None
    bytes_sent = int(raw_bytes)
    if bytes_sent < 0:
        return None

    method = match.group("method")
    path = match.group("path")
    protocol = match.group("protocol")
    if not method or not path or not protocol:
        return None

    return LogRecord(
        remote_addr=match.group("remote_addr"),
        timestamp=ts,
        method=method,
        path=path,
        protocol=protocol,
        status=status,
        bytes_sent=bytes_sent,
    )


def iter_file_lines(path: Path) -> Iterator[str]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            yield line


def classify_status(status: int) -> str:
    if 400 <= status <= 499:
        return "client_error"
    if 500 <= status <= 599:
        return "server_error"
    return "non_error"


def hour_key(ts: datetime) -> datetime:
    """Trunca a la hora conservando tzinfo (el offset del registro)."""
    return ts.replace(minute=0, second=0, microsecond=0)


def aggregate(path: Path) -> Aggregation:
    agg = Aggregation()
    for line in iter_file_lines(path):
        agg.total_lines += 1
        record = parse_line(line)
        if record is None:
            agg.invalid_records += 1
            continue

        agg.valid_records += 1
        category = classify_status(record.status)
        if category == "non_error":
            agg.non_error += 1
            continue

        bucket = agg.hourly.setdefault(hour_key(record.timestamp), HourBucket())
        if category == "client_error":
            agg.client_errors += 1
            agg.error_codes[record.status] += 1
            bucket.client_errors += 1
        else:  # server_error
            agg.server_errors += 1
            agg.error_codes[record.status] += 1
            bucket.server_errors += 1
    return agg


def format_reason(code: int) -> str:
    reason = _HTTP_REASONS.get(code)
    return f"{code} {reason}" if reason else f"HTTP {code} (unknown reason)"


def format_hour_label(dt: datetime) -> str:
    # dt conserva tzinfo con offset tipo -0500; strftime nos da el sufijo.
    return dt.strftime("%Y-%m-%d %H:%M %z")


def sort_key(item: tuple[datetime, HourBucket]) -> tuple[datetime, str]:
    """Orden cronológico por instante absoluto; desempate estable por etiqueta."""
    dt = item[0]
    return (dt.astimezone(timezone.utc), format_hour_label(dt))


def print_report(file_path: Path, agg: Aggregation) -> None:
    print("NGINX LOG ANALYZER")
    print("--------------------------------")
    print(f"File: {file_path}")
    print()
    print("PARSING SUMMARY")
    print(f"Total lines:     {agg.total_lines:>6}")
    print(f"Valid records:   {agg.valid_records:>6}")
    print(f"Invalid records: {agg.invalid_records:>6}")
    print()
    print("HTTP ERROR SUMMARY")
    print(f"Non-error responses:  {agg.non_error:>6}")
    print(f"Client errors (4xx):  {agg.client_errors:>6}")
    print(f"Server errors (5xx):  {agg.server_errors:>6}")
    print(f"Total HTTP errors:    {agg.total_http_errors:>6}")

    if agg.total_http_errors == 0:
        print()
        print("No HTTP errors were found.")
        print("--------------------------------")
        print("Analysis completed.")
        return

    print()
    print("HTTP ERROR DISTRIBUTION")
    for code in sorted(agg.error_codes):
        print(f"{format_reason(code)}: {agg.error_codes[code]:>6}")

    print()
    print("HOURLY HTTP ERROR STATISTICS")
    print("-" * 53)
    print(f"{'DATE / HOUR':<28}{'4xx':>6}{'5xx':>6}{'TOTAL':>8}")
    ordered = sorted(agg.hourly.items(), key=sort_key)
    for dt, bucket in ordered:
        print(
            f"{format_hour_label(dt):<28}"
            f"{bucket.client_errors:>6}"
            f"{bucket.server_errors:>6}"
            f"{bucket.total:>8}"
        )
    print("-" * 53)

    # max() sobre lista ya ordenada cronológicamente -> primer máximo en empate.
    peak_dt, peak_bucket = max(ordered, key=lambda kv: kv[1].total)
    print(f"Peak error hour: {format_hour_label(peak_dt)}")
    print(f"Peak error count: {peak_bucket.total}")
    print("(ties resolved to the earliest interval)")
    print("--------------------------------")
    print("Analysis completed.")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="log_summary.py",
        description=(
            "Parsea un access log de Nginx (combined), cuenta errores HTTP "
            "4xx/5xx globales y por intervalo horario."
        ),
    )
    parser.add_argument(
        "--file",
        required=True,
        type=Path,
        help="Ruta al archivo de log de Nginx (combined).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    file_path: Path = args.file

    if not file_path.exists():
        print(f"ERROR: el archivo no existe: {file_path}", file=sys.stderr)
        return EXIT_READ_ERROR
    if not file_path.is_file():
        print(f"ERROR: la ruta no es un archivo regular: {file_path}", file=sys.stderr)
        return EXIT_READ_ERROR

    try:
        agg = aggregate(file_path)
    except PermissionError:
        print(f"ERROR: no se puede leer el archivo: {file_path}", file=sys.stderr)
        return EXIT_READ_ERROR
    except UnicodeDecodeError as exc:
        print(
            f"ERROR: el archivo no está en UTF-8 válido: {file_path} "
            f"(byte {exc.start})",
            file=sys.stderr,
        )
        return EXIT_READ_ERROR
    except OSError as exc:
        print(f"ERROR: fallo al leer el archivo: {exc}", file=sys.stderr)
        return EXIT_READ_ERROR

    print_report(file_path, agg)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())