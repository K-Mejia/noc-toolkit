#!/usr/bin/env python3
"""Nginx access log parser (combined format) — NOC Toolkit.

Etapa DEV-004.1: solo lectura, parsing y validación de líneas.
No se calculan estadísticas de códigos, métodos, IPs ni horas.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

EXIT_OK = 0
EXIT_READ_ERROR = 1
EXIT_USAGE = 2

# Formato combined:
# $remote_addr - $remote_user [$time_local] "$request" $status $body_bytes_sent
#   "$http_referer" "$http_user_agent"
#
# - remote_addr: IP o nombre (no validamos contenido aquí; validación de IP
#   estricta no está en alcance de esta etapa).
# - remote_user: '-' o usuario sin espacios.
# - time_local: dentro de [...]; lo validamos con datetime después.
# - request: "METODO RUTA PROTOCOLO"; sin espacios en cada token.
# - status: 3 dígitos (rango 100-599 validado después).
# - body_bytes_sent: dígitos o '-' (Nginx puede emitir '-' en logs truncados).
# - referer, user-agent: entre comillas dobles, sin comillas internas.
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

# Formatos de fecha aceptados en Nginx (time_local).
_DT_FORMAT = "%d/%b/%Y:%H:%M:%S %z"


@dataclass(frozen=True)
class LogRecord:
    remote_addr: str
    timestamp: datetime
    method: str
    path: str
    protocol: str
    status: int
    bytes_sent: int


def parse_timestamp(raw: str) -> datetime | None:
    """Devuelve datetime con tzinfo o None si el formato es inválido."""
    try:
        return datetime.strptime(raw, _DT_FORMAT)
    except ValueError:
        return None


def parse_line(line: str) -> LogRecord | None:
    """Parsea una línea combined. Devuelve None si es inválida.

    La línea puede venir con o sin salto final; se limpia antes de parsear.
    """
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
        # Nginx puede emitir '-' si el tamaño no está disponible.
        # Lo tratamos como inválido en esta etapa para forzar revisión
        # explícita en un incremento futuro. Ver limitaciones.
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
    """Itera líneas en UTF-8. Propaga UnicodeDecodeError y OSError."""
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            yield line


def process_file(path: Path) -> tuple[int, int, int]:
    """Devuelve (total_lines, valid_records, invalid_records)."""
    total = 0
    valid = 0
    invalid = 0
    for line in iter_file_lines(path):
        # Contar todas las líneas no vacías como registros intentados.
        # Líneas vacías o solo espacios se cuentan como inválidas.
        total += 1
        if parse_line(line) is None:
            invalid += 1
        else:
            valid += 1
    return total, valid, invalid


def print_report(file_path: Path, total: int, valid: int, invalid: int) -> None:
    print("NGINX LOG PARSER")
    print("--------------------------------")
    print(f"File: {file_path}")
    print()
    print(f"Total lines:     {total:>6}")
    print(f"Valid records:   {valid:>6}")
    print(f"Invalid records: {invalid:>6}")
    print("--------------------------------")
    print("Parsing completed.")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="log_summary.py",
        description=(
            "Parsea un access log de Nginx en formato combined y muestra "
            "un resumen básico de registros válidos e inválidos."
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
    # argparse llama a sys.exit(2) si --file falta o es inválido,
    # lo que respeta nuestro EXIT_USAGE=2.
    args = parser.parse_args(argv)

    file_path: Path = args.file

    if not file_path.exists():
        print(f"ERROR: el archivo no existe: {file_path}", file=sys.stderr)
        return EXIT_READ_ERROR
    if not file_path.is_file():
        print(f"ERROR: la ruta no es un archivo regular: {file_path}", file=sys.stderr)
        return EXIT_READ_ERROR

    try:
        total, valid, invalid = process_file(file_path)
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

    print_report(file_path, total, valid, invalid)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())