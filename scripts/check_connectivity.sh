#!/usr/bin/env bash
#
# check_connectivity.sh - NOC Toolkit
# Etapa 2.1: parser y validador de configuración de hosts (CSV).
#
# En esta etapa NO se realizan comprobaciones de red (ping/DNS/TCP).
#

set -u

# ---------------------------------------------------------------------------
# Códigos de salida
# ---------------------------------------------------------------------------
readonly EXIT_OK=0
readonly EXIT_USAGE=1
readonly EXIT_FILE=2
readonly EXIT_FORMAT=3

# ---------------------------------------------------------------------------
# Resultado del parser (disponible para futuras etapas)
# ---------------------------------------------------------------------------
PARSED_HOSTS=()
PARSED_PORTS=()

# ---------------------------------------------------------------------------
# Uso / ayuda
# ---------------------------------------------------------------------------
usage() {
    cat <<'EOF'
Uso: check_connectivity.sh --file <archivo.csv>

Descripción:
  Lee y valida un archivo CSV con encabezado "host,port".
  En esta versión solo se valida la configuración; no se comprueba
  conectividad.

Opciones:
  --file <ruta>   Archivo CSV de entrada (obligatorio).
  -h, --help      Muestra esta ayuda y termina.

Formato del CSV:
  - Primera línea: cabecera exacta "host,port".
  - Cada línea siguiente: "<host>,<puerto>".
  - Líneas vacías permitidas (se ignoran).
  - Solo dos columnas, sin comillas ni comas escapadas.

Ejemplo:
  check_connectivity.sh --file config/hosts.csv

Códigos de salida:
  0  Configuración válida.
  1  Error de uso (argumentos).
  2  Error de archivo (no existe, no legible o vacío).
  3  Error de formato (cabecera, host o puerto inválido).

Limitaciones actuales:
  - No admite campos CSV entrecomillados ni comas escapadas.
  - No admite cabeceras con espacios ni columnas en otro orden.
EOF
}

# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------
err() {
    printf 'ERROR: %s\n' "$*" >&2
}

# ---------------------------------------------------------------------------
# Validación de argumentos. Deja el valor en la variable global ARG_FILE.
# Devuelve EXIT_OK o EXIT_USAGE (no imprime por stdout).
# ---------------------------------------------------------------------------
parse_args() {
    ARG_FILE=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --file)
                if [[ $# -lt 2 || -z "${2:-}" ]]; then
                    err "La opción --file requiere un argumento."
                    return "$EXIT_USAGE"
                fi
                if [[ -n "$ARG_FILE" ]]; then
                    err "La opción --file se especificó más de una vez."
                    return "$EXIT_USAGE"
                fi
                ARG_FILE="$2"
                shift 2
                ;;
            -h|--help)
                usage
                exit "$EXIT_OK"
                ;;
            --)
                shift
                if [[ $# -gt 0 ]]; then
                    err "Argumentos no soportados: $*"
                    return "$EXIT_USAGE"
                fi
                ;;
            *)
                err "Argumento desconocido: $1"
                return "$EXIT_USAGE"
                ;;
        esac
    done

    if [[ -z "$ARG_FILE" ]]; then
        err "Falta la opción obligatoria --file."
        return "$EXIT_USAGE"
    fi

    return "$EXIT_OK"
}

# ---------------------------------------------------------------------------
# Comprueba que el archivo exista y sea legible y no vacío.
# Devuelve EXIT_OK o EXIT_FILE.
# ---------------------------------------------------------------------------
check_file_readable() {
    local file="$1"

    if [[ ! -e "$file" ]]; then
        err "El archivo no existe: $file"
        return "$EXIT_FILE"
    fi

    if [[ ! -f "$file" ]]; then
        err "La ruta no es un archivo regular: $file"
        return "$EXIT_FILE"
    fi

    if [[ ! -r "$file" ]]; then
        err "El archivo no es legible: $file"
        return "$EXIT_FILE"
    fi

    if [[ ! -s "$file" ]]; then
        err "El archivo está vacío: $file"
        return "$EXIT_FILE"
    fi

    return "$EXIT_OK"
}

# ---------------------------------------------------------------------------
# Valida que un puerto sea entero entre 1 y 65535.
# ---------------------------------------------------------------------------
is_valid_port() {
    local port="$1"

    [[ "$port" =~ ^[0-9]+$ ]] || return 1
    # Evita problemas de interpretación octal en comparaciones aritméticas.
    (( 10#$port >= 1 && 10#$port <= 65535 ))
}

# ---------------------------------------------------------------------------
# Parsea y valida el CSV. Rellena PARSED_HOSTS y PARSED_PORTS.
# Devuelve EXIT_OK o EXIT_FORMAT.
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

        # Ignorar líneas vacías (no cuentan como registros).
        if [[ -z "${line//[[:space:]]/}" ]]; then
            continue
        fi

        # Normalizar fin de línea CRLF por si el CSV viene de Windows.
        line="${line%$'\r'}"

        if (( header_seen == 0 )); then
            if [[ "$line" != "host,port" ]]; then
                err "Cabecera inválida en línea $line_no: se esperaba 'host,port'."
                return "$EXIT_FORMAT"
            fi
            header_seen=1
            continue
        fi

        # Separar en 3 campos para detectar columnas extra.
        IFS=',' read -r host port extra <<< "$line"

        if [[ -n "${extra:-}" ]]; then
            err "Registro con más de dos columnas en línea $line_no."
            return "$EXIT_FORMAT"
        fi

        # Detectar ausencia de coma (una sola columna).
        if [[ "$line" != *,* ]]; then
            err "Registro malformado en línea $line_no: falta la coma separadora."
            return "$EXIT_FORMAT"
        fi

        if [[ -z "$host" ]]; then
            err "Host vacío en línea $line_no."
            return "$EXIT_FORMAT"
        fi

        if ! is_valid_port "$port"; then
            err "Puerto inválido en línea $line_no: '$port' (debe ser entero 1-65535)."
            return "$EXIT_FORMAT"
        fi

        PARSED_HOSTS+=("$host")
        PARSED_PORTS+=("$port")
        records=$((records + 1))
    done < "$file"

    if (( header_seen == 0 )); then
        err "El archivo no contiene cabecera 'host,port'."
        return "$EXIT_FORMAT"
    fi

    if (( records == 0 )); then
        err "El archivo no contiene registros válidos."
        return "$EXIT_FORMAT"
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

    # Resumen mínimo para verificación manual (la salida definitiva se
    # definirá cuando existan ping/DNS/TCP).
    printf 'Configuración válida: %d registro(s) en %s\n' \
        "${#PARSED_HOSTS[@]}" "$ARG_FILE"

    return "$EXIT_OK"
}

main "$@"
exit $?