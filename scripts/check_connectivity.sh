#!/usr/bin/env bash
#
# check_connectivity.sh - NOC Toolkit
# Etapa 2.2: parser CSV + verificaciones ICMP y DNS.
#

set -u

# ---------------------------------------------------------------------------
# Códigos de salida
# ---------------------------------------------------------------------------
readonly EXIT_OK=0
readonly EXIT_CONNECTIVITY=1
readonly EXIT_CONFIG=2

# ---------------------------------------------------------------------------
# Resultado del parser (disponible para las verificaciones)
# ---------------------------------------------------------------------------
PARSED_HOSTS=()
PARSED_PORTS=()

# ---------------------------------------------------------------------------
# Configuración de ejecución
# ---------------------------------------------------------------------------
ARG_FILE=""
TIMEOUT_SECS=2

# ---------------------------------------------------------------------------
# Resultados por host (arrays paralelos)
# ---------------------------------------------------------------------------
RESULT_ICMP=()
RESULT_DNS=()

# ---------------------------------------------------------------------------
# Uso / ayuda
# ---------------------------------------------------------------------------
usage() {
    cat <<'EOF'
Uso: check_connectivity.sh --file <archivo.csv> [--timeout <segundos>]

Descripción:
  Lee un archivo CSV con encabezado "host,port", comprueba resolución DNS
  (getent ahosts) y conectividad ICMP (ping) de cada host, y muestra un
  informe.

Opciones:
  --file <ruta>        Archivo CSV de entrada (obligatorio).
  --timeout <segundos> Timeout por comprobación ICMP. Entero positivo.
                       Predeterminado: 2.
  -h, --help           Muestra esta ayuda y termina.

Formato del CSV:
  - Primera línea: cabecera exacta "host,port".
  - Cada línea siguiente: "<host>,<puerto>".
  - Líneas vacías permitidas (se ignoran).
  - Solo dos columnas, sin comillas ni comas escapadas.

Comportamiento DNS:
  - Hosts que son IP literal: DNS = N/A (no se hace DNS inverso).
  - Hosts con nombre: DNS = OK si getent ahosts devuelve al menos una
    dirección; FAIL en caso contrario.

Comportamiento ICMP:
  - IP literal: se hace ping directamente.
  - Nombre con DNS OK: se hace ping al nombre.
  - Nombre con DNS FAIL: ICMP = FAIL sin ejecutar ping (dependencia técnica).

Códigos de salida:
  0  Configuración válida y todas las comprobaciones aplicables OK.
  1  Configuración válida, pero alguna comprobación de conectividad falló.
  2  Error de uso, de configuración o de entorno.

Limitaciones actuales:
  - No se verifica TCP (puertos del CSV aún no se usan).
  - getent ahosts depende de /etc/nsswitch.conf y de la configuración NSS.
  - ping -W puede no ser interpretado igual por implementaciones no-iputils.
EOF
}

# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------
err() {
    printf 'ERROR: %s\n' "$*" >&2
}

# ---------------------------------------------------------------------------
# Validación de argumentos
# ---------------------------------------------------------------------------
parse_args() {
    ARG_FILE=""
    TIMEOUT_SECS=2

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --file)
                if [[ $# -lt 2 || -z "${2:-}" ]]; then
                    err "La opción --file requiere un argumento."
                    return "$EXIT_CONFIG"
                fi
                if [[ -n "$ARG_FILE" ]]; then
                    err "La opción --file se especificó más de una vez."
                    return "$EXIT_CONFIG"
                fi
                ARG_FILE="$2"
                shift 2
                ;;
            --timeout)
                if [[ $# -lt 2 || -z "${2:-}" ]]; then
                    err "La opción --timeout requiere un argumento."
                    return "$EXIT_CONFIG"
                fi
                if [[ ! "$2" =~ ^[0-9]+$ ]] || (( 10#$2 < 1 )); then
                    err "Timeout inválido: '$2' (debe ser entero positivo)."
                    return "$EXIT_CONFIG"
                fi
                TIMEOUT_SECS=$((10#$2))
                shift 2
                ;;
            -h|--help)
                usage
                exit "$EXIT_OK"
                ;;
            --)
                shift
                if [[ $# -gt 0 ]]; then
                    err "Argumentos posicionales no soportados: $*"
                    return "$EXIT_CONFIG"
                fi
                ;;
            *)
                err "Argumento desconocido: $1"
                return "$EXIT_CONFIG"
                ;;
        esac
    done

    if [[ -z "$ARG_FILE" ]]; then
        err "Falta la opción obligatoria --file."
        return "$EXIT_CONFIG"
    fi

    return "$EXIT_OK"
}

# ---------------------------------------------------------------------------
# Disponibilidad de comandos externos
# ---------------------------------------------------------------------------
check_dependencies() {
    local cmd
    for cmd in ping getent timeout; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            err "Comando requerido no encontrado en PATH: $cmd"
            return "$EXIT_CONFIG"
        fi
    done
    return "$EXIT_OK"
}

# ---------------------------------------------------------------------------
# Comprueba que el archivo exista y sea legible y no vacío.
# ---------------------------------------------------------------------------
check_file_readable() {
    local file="$1"

    if [[ ! -e "$file" ]]; then
        err "El archivo no existe: $file"
        return "$EXIT_CONFIG"
    fi
    if [[ ! -f "$file" ]]; then
        err "La ruta no es un archivo regular: $file"
        return "$EXIT_CONFIG"
    fi
    if [[ ! -r "$file" ]]; then
        err "El archivo no es legible: $file"
        return "$EXIT_CONFIG"
    fi
    if [[ ! -s "$file" ]]; then
        err "El archivo está vacío: $file"
        return "$EXIT_CONFIG"
    fi
    return "$EXIT_OK"
}

# ---------------------------------------------------------------------------
# Validación de puerto (se conserva del parser; aún no se usa en red)
# ---------------------------------------------------------------------------
is_valid_port() {
    local port="$1"
    [[ "$port" =~ ^[0-9]+$ ]] || return 1
    (( 10#$port >= 1 && 10#$port <= 65535 ))
}

# ---------------------------------------------------------------------------
# Parser CSV (sin cambios de contrato)
# ---------------------------------------------------------------------------
parse_hosts_file() {
    local file="$1"
    local line line_no=0
    local header_seen=0
    local records=0
    local host port extra

    PARSED_HOSTS=()
    PARSED_PORTS=()

    while IFS= read -r line || [[ -n "$line" ]]; do
        line_no=$((line_no + 1))

        if [[ -z "${line//[[:space:]]/}" ]]; then
            continue
        fi

        line="${line%$'\r'}"

        if (( header_seen == 0 )); then
            if [[ "$line" != "host,port" ]]; then
                err "Cabecera inválida en línea $line_no: se esperaba 'host,port'."
                return "$EXIT_CONFIG"
            fi
            header_seen=1
            continue
        fi

        IFS=',' read -r host port extra <<< "$line"

        if [[ -n "${extra:-}" ]]; then
            err "Registro con más de dos columnas en línea $line_no."
            return "$EXIT_CONFIG"
        fi
        if [[ "$line" != *,* ]]; then
            err "Registro malformado en línea $line_no: falta la coma separadora."
            return "$EXIT_CONFIG"
        fi
        if [[ -z "$host" ]]; then
            err "Host vacío en línea $line_no."
            return "$EXIT_CONFIG"
        fi
        if ! is_valid_port "$port"; then
            err "Puerto inválido en línea $line_no: '$port' (debe ser entero 1-65535)."
            return "$EXIT_CONFIG"
        fi

        PARSED_HOSTS+=("$host")
        PARSED_PORTS+=("$port")
        records=$((records + 1))
    done < "$file"

    if (( header_seen == 0 )); then
        err "El archivo no contiene cabecera 'host,port'."
        return "$EXIT_CONFIG"
    fi
    if (( records == 0 )); then
        err "El archivo no contiene registros válidos."
        return "$EXIT_CONFIG"
    fi
    return "$EXIT_OK"
}

# ---------------------------------------------------------------------------
# Detección de IP literal (IPv4 o IPv6). No hace consultas de red.
# ---------------------------------------------------------------------------
is_ip_literal() {
    local h="$1"
    # IPv4
    if [[ "$h" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
        local o1 o2 o3 o4
        IFS='.' read -r o1 o2 o3 o4 <<< "$h"
        (( 10#$o1 <= 255 && 10#$o2 <= 255 && 10#$o3 <= 255 && 10#$o4 <= 255 ))
        return $?
    fi
    # IPv6 (forma simplificada: contiene ':' y solo hex/':')
    if [[ "$h" == *:* && "$h" =~ ^[0-9A-Fa-f:]+$ ]]; then
        return 0
    fi
    return 1
}

# ---------------------------------------------------------------------------
# DNS: OK / FAIL / N/A. Sin salida cruda de getent.
# ---------------------------------------------------------------------------
check_dns() {
    local host="$1"

    if is_ip_literal "$host"; then
        printf 'N/A'
        return
    fi

    if getent ahosts "$host" 2>/dev/null | grep -qE '^[0-9]'; then
        printf 'OK'
    else
        printf 'FAIL'
    fi
}

# ---------------------------------------------------------------------------
# ICMP: OK / FAIL. Usa ping -W y un timeout externo como salvaguarda.
# ---------------------------------------------------------------------------
check_icmp() {
    local host="$1"

    # Si no es IP y DNS falló, no intentamos ping contra un nombre irresoluble.
    if ! is_ip_literal "$host"; then
        if ! getent ahosts "$host" 2>/dev/null | grep -qE '^[0-9]'; then
            printf 'FAIL'
            return
        fi
    fi

    if timeout "$TIMEOUT_SECS" ping -W "$TIMEOUT_SECS" -c 1 "$host" >/dev/null 2>&1; then
        printf 'OK'
    else
        printf 'FAIL'
    fi
}

# ---------------------------------------------------------------------------
# Ejecuta verificaciones para todos los hosts y llena RESULT_*.
# ---------------------------------------------------------------------------
run_checks() {
    local i
    RESULT_ICMP=()
    RESULT_DNS=()

    for i in "${!PARSED_HOSTS[@]}"; do
        RESULT_DNS+=("$(check_dns "${PARSED_HOSTS[$i]}")")
        RESULT_ICMP+=("$(check_icmp "${PARSED_HOSTS[$i]}")")
    done
}

# ---------------------------------------------------------------------------
# Presentación
# ---------------------------------------------------------------------------
print_report() {
    local i
    local total=${#PARSED_HOSTS[@]}
    local icmp_ok=0 icmp_fail=0 dns_fail=0

    for i in "${!PARSED_HOSTS[@]}"; do
        case "${RESULT_ICMP[$i]}" in
            OK) icmp_ok=$((icmp_ok + 1)) ;;
            FAIL) icmp_fail=$((icmp_fail + 1)) ;;
        esac
        case "${RESULT_DNS[$i]}" in
            FAIL) dns_fail=$((dns_fail + 1)) ;;
        esac
    done

    printf '\n[+] NETWORK CONNECTIVITY REPORT\n\n\n\n'
    printf '%-18s %-8s %-8s\n' "HOST" "ICMP" "DNS"
    printf '%s\n' "---------------------------------"
    for i in "${!PARSED_HOSTS[@]}"; do
        printf '%-18s %-8s %-8s\n' \
            "${PARSED_HOSTS[$i]}" "${RESULT_ICMP[$i]}" "${RESULT_DNS[$i]}"
    done

    printf '\nSUMMARY\n'
    printf 'Total hosts: %d\n' "$total"
    printf 'ICMP success: %d\n' "$icmp_ok"
    printf 'ICMP failures: %d\n' "$icmp_fail"
    printf 'DNS failures: %d\n' "$dns_fail"

    if (( icmp_fail > 0 )); then
        return "$EXIT_CONNECTIVITY"
    fi
    return "$EXIT_OK"
}

# ---------------------------------------------------------------------------
# Programa principal
# ---------------------------------------------------------------------------
main() {
    local rc

    parse_args "$@"
    rc=$?
    if (( rc != 0 )); then
        usage >&2
        return "$rc"
    fi

    check_dependencies
    rc=$?
    if (( rc != 0 )); then
        return "$rc"
    fi

    check_file_readable "$ARG_FILE"
    rc=$?
    if (( rc != 0 )); then
        return "$rc"
    fi

    parse_hosts_file "$ARG_FILE"
    rc=$?
    if (( rc != 0 )); then
        return "$rc"
    fi

    run_checks
    print_report
    return $?
}

main "$@"
exit $?