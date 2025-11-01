#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${SCRIPT_DIR}"
SERVICE_TEMPLATE="${PROJECT_ROOT}/hall-frontline-pass.service.dist"
SERVICE_TARGET="/etc/systemd/system/hall-frontline-pass@.service"
PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python"
SERVICE_USER="${BOT_SERVICE_USER:-$(id -un)}"
SERVICE_NAME="hall-frontline-pass@${SERVICE_USER}"

print_usage() {
    cat <<EOF
Usage: ${0##*/} <command>

Commands:
  install   Copy the systemd template and enable ${SERVICE_NAME}
  start     Start the bot via systemd
  stop      Stop the bot via systemd
  restart   Restart the bot via systemd
  status    Show systemd status for ${SERVICE_NAME}
  run       Run the bot directly in the foreground (development)

Environment variables:
  BOT_SERVICE_USER   Override the user portion of ${SERVICE_NAME} (default: current user)
EOF
}

need_systemctl() {
    if ! command -v systemctl >/dev/null 2>&1; then
        echo "systemctl is required for this command. Is this a systemd host?" >&2
        exit 1
    fi
}

maybe_with_sudo() {
    if command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        "$@"
    fi
}

service_template_present() {
    [[ -f "${SERVICE_TEMPLATE}" ]]
}

service_file_installed() {
    [[ -f "${SERVICE_TARGET}" ]]
}

ensure_service_file() {
    if ! service_file_installed; then
        echo "Systemd unit ${SERVICE_TARGET} is missing. Run '${0##*/} install' first." >&2
        exit 1
    fi
}

install_service() {
    need_systemctl
    if ! service_template_present; then
        echo "Service template ${SERVICE_TEMPLATE} not found." >&2
        exit 1
    fi
    maybe_with_sudo install -m 0644 "${SERVICE_TEMPLATE}" "${SERVICE_TARGET}"
    maybe_with_sudo systemctl daemon-reload
    maybe_with_sudo systemctl enable "${SERVICE_NAME}"
    echo "Installed ${SERVICE_TARGET} and enabled ${SERVICE_NAME}."
    echo "You can now run '${0##*/} start' to launch the bot."
}

systemctl_cmd() {
    need_systemctl
    ensure_service_file
    maybe_with_sudo systemctl "$@"
}

case "${1:-}" in
    install)
        install_service
        ;;
    start)
        systemctl_cmd start "${SERVICE_NAME}"
        ;;
    stop)
        systemctl_cmd stop "${SERVICE_NAME}"
        ;;
    restart)
        systemctl_cmd restart "${SERVICE_NAME}"
        ;;
    status)
        systemctl_cmd status "${SERVICE_NAME}"
        ;;
    run)
        if [[ ! -x "${PYTHON_BIN}" ]]; then
            echo "Python interpreter not found at ${PYTHON_BIN}. Did you create the virtual environment?" >&2
            exit 1
        fi
        cd "${PROJECT_ROOT}"
        exec "${PYTHON_BIN}" frontline-pass.py
        ;;
    ""|-h|--help)
        print_usage
        ;;
    *)
        echo "Unknown command: ${1}" >&2
        print_usage
        exit 1
        ;;
esac
