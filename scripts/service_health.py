#!/usr/bin/env python3
"""Systemd service health checker — NOC Toolkit.

Etapa DEV-005.1: consulta el estado de servicios vía `systemctl show`
y emite un reporte JSON. Las pruebas automatizadas llegan en DEV-005.2.
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Iterable

EXIT_OK = 0
EXIT_UNHEALTHY = 1
EXIT_ERROR = 2

DEFAULT_TIMEOUT = 5.0

_PROPERTIES = ("LoadState", "ActiveState", "SubState")


@dataclass
class ServiceStatus:
    name: str
    resolved_name: str
    load_state: str
    active_state: str
    sub_state: str
    healthy: bool


# ---------------------------------------------------------------------------
# Consulta a systemctl
# ---------------------------------------------------------------------------

def query_systemctl(name: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, str] | None:
    """Ejecuta `systemctl show <name>` y devuelve dict de propiedades.

    Devuelve None si systemctl no está disponible, si hubo timeout o si
    el comando falló. No lanza excepciones al llamador.
    """
    args = ["systemctl", "show", name]
    for prop in _PROPERTIES:
        args.append(f"--property={prop}")

    try:
        cp = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"WARN: fallo al ejecutar systemctl para '{name}': {exc}", file=sys.stderr)
        return None

    # systemctl show devuelve 0 incluso para not-found en muchas versiones,
    # pero si devuelve != 0 y no hay propiedades, tratamos como error.
    props: dict[str, str] = {}
    for line in cp.stdout.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key in _PROPERTIES:
            props[key] = value

    if not props:
        return None
    return props


def resolve_and_query(name: str, timeout: float = DEFAULT_TIMEOUT) -> tuple[str, dict[str, str] | None]:
    """Consulta `name`; si no se encuentra y no lleva `.service`, reintenta
    con `name + ".service"`. Devuelve (nombre_resuelto, props | None)."""
    props = query_systemctl(name, timeout=timeout)
    if props is not None and props.get("LoadState") != "not-found":
        return name, props

    if not name.endswith(".service"):
        alt = f"{name}.service"
        alt_props = query_systemctl(alt, timeout=timeout)
        if alt_props is not None and alt_props.get("LoadState") != "not-found":
            return alt, alt_props

    # Ni el original ni el alternativo existen (o fallaron las consultas).
    return name, props


def build_service_status(name: str, timeout: float = DEFAULT_TIMEOUT) -> ServiceStatus:
    resolved, props = resolve_and_query(name, timeout=timeout)

    if props is None:
        # No se pudo consultar: estado desconocido, no saludable.
        return ServiceStatus(
            name=name,
            resolved_name=resolved,
            load_state="unknown",
            active_state="unknown",
            sub_state="unknown",
            healthy=False,
        )

    load_state = props.get("LoadState", "unknown")
    active_state = props.get("ActiveState", "unknown")
    sub_state = props.get("SubState", "unknown")
    healthy = (active_state == "active")
    return ServiceStatus(
        name=name,
        resolved_name=resolved,
        load_state=load_state,
        active_state=active_state,
        sub_state=sub_state,
        healthy=healthy,
    )


# ---------------------------------------------------------------------------
# Reporte
# ---------------------------------------------------------------------------

def build_report(services: Iterable[str], timeout: float = DEFAULT_TIMEOUT) -> dict:
    statuses = [build_service_status(s, timeout=timeout) for s in services]
    healthy = sum(1 for s in statuses if s.healthy)
    return {
        "hostname": socket.gethostname(),
        "total_services": len(statuses),
        "healthy_count": healthy,
        "unhealthy_count": len(statuses) - healthy,
        "services": [asdict(s) for s in statuses],
    }


def exit_code_for(report: dict) -> int:
    return EXIT_OK if report["unhealthy_count"] == 0 else EXIT_UNHEALTHY


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="service_health.py",
        description=(
            "Consulta el estado de servicios systemd y emite un reporte JSON. "
            "En esta versión --json es obligatorio."
        ),
    )
    parser.add_argument(
        "--services",
        nargs="+",
        required=True,
        metavar="SERVICE",
        help="Uno o más nombres de servicios systemd (ej: nginx ssh cron).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emite el reporte en JSON (obligatorio en esta versión).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if not args.services:
        print("ERROR: --services no puede estar vacío.", file=sys.stderr)
        return EXIT_ERROR

    if not args.json:
        parser.print_help(sys.stderr)
        print("ERROR: en esta versión --json es obligatorio.", file=sys.stderr)
        return EXIT_ERROR

    if shutil.which("systemctl") is None:
        print("ERROR: systemctl no está disponible en PATH.", file=sys.stderr)
        return EXIT_ERROR

    report = build_report(args.services)
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return exit_code_for(report)


if __name__ == "__main__":
    sys.exit(main())