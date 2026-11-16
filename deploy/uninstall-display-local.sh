#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != "Linux" ] || ! command -v systemctl >/dev/null 2>&1; then
  echo "Display uninstaller requires Linux with systemd." >&2
  exit 1
fi

# Until the layout is known, nothing has changed.
trap 'echo "Uninstall was stopped. No changes made." >&2; exit 129' HUP
trap 'echo "Uninstall was stopped. No changes made." >&2; exit 130' INT
trap 'echo "Uninstall was stopped. No changes made." >&2; exit 143' TERM
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if ! native_names=$(python3 "${root}/src/inky_bird_frame/native_names.py"); then
  echo "Could not find where the services are installed. No changes made." >&2
  exit 1
fi
service_prefix="" app_dir="" support_dir="" config_path=""
eval "${native_names}"
# A stopped uninstall names what it has not removed yet.
stopped() {
  trap '' HUP INT TERM
  local remaining=() unit
  for unit in "${service_prefix}-display.service" "${service_prefix}-display.timer"; do
    if [ -e "/etc/systemd/system/${unit}" ]; then
      remaining+=("${unit}")
    fi
  done
  echo "Uninstall was stopped before it finished; still installed: ${remaining[*]-none}." >&2
  echo "Run the uninstaller again to finish." >&2
  exit "$1"
}
trap 'stopped 129' HUP
trap 'stopped 130' INT
trap 'stopped 143' TERM
venv=${INKY_BIRD_DISPLAY_VENV:-"${HOME}/.virtualenvs/pimoroni"}
noninteractive_sudo=${INKY_BIRD_NONINTERACTIVE_SUDO:-false}

sudo_command=(sudo)
case "${noninteractive_sudo}" in
  true)
    sudo_command+=(-n)
    ;;
  false)
    if ! sudo -v; then
      echo "Administrator access is required to remove the display service. No changes made." >&2
      exit 1
    fi
    ;;
  *)
    echo "INKY_BIRD_NONINTERACTIVE_SUDO must be true or false." >&2
    exit 1
    ;;
esac

failures=()
not_stopped() {
  local failed
  for failed in "${failures[@]+"${failures[@]}"}"; do
    [ "${failed}" = "$1" ] && return 0
  done
  return 1
}
installed() {
  [ "$(systemctl show --property=LoadState --value "$1" 2>/dev/null || true)" != "not-found" ] || \
    [ -e "/etc/systemd/system/$1" ]
}
if installed "${service_prefix}-display.timer"; then
  if "${sudo_command[@]}" systemctl disable --now "${service_prefix}-display.timer"; then
    echo "Stopped and disabled ${service_prefix}-display.timer."
  else
    failures+=("${service_prefix}-display.timer")
  fi
fi
if installed "${service_prefix}-display.service"; then
  if "${sudo_command[@]}" systemctl stop "${service_prefix}-display.service"; then
    echo "Stopped ${service_prefix}-display.service."
  else
    failures+=("${service_prefix}-display.service")
  fi
fi

for unit in "${service_prefix}-display.service" "${service_prefix}-display.timer"; do
  # Keep the file of a unit that may still run so a rerun finds and stops it,
  # and the service of a timer that may still start it.
  if not_stopped "${unit}" || not_stopped "${unit%.service}.timer"; then
    continue
  fi
  if [ -f "/etc/systemd/system/${unit}" ]; then
    if "${sudo_command[@]}" rm -f "/etc/systemd/system/${unit}"; then
      echo "Removed /etc/systemd/system/${unit}."
    else
      failures+=("/etc/systemd/system/${unit}")
    fi
  fi
done
if "${sudo_command[@]}" systemctl daemon-reload; then
  echo "Reloaded the systemd unit configuration."
else
  failures+=("systemd unit reload")
fi

if [ "${#failures[@]}" -gt 0 ]; then
  echo "Uninstall did not finish. Could not stop or remove: ${failures[*]}." >&2
  echo "Fix the cause shown above and run the uninstaller again." >&2
  exit 1
fi
echo "Display node services uninstalled."
echo "Intentionally left in place:"
echo "  Application runtime and catalog copy: ${app_dir}"
echo "  Configuration: ${config_path}"
echo "  Support data: ${support_dir}"
echo "  Pimoroni Python environment: ${venv}"
echo "  Display state directory configured in ${config_path}"
echo "Remove those paths manually if you no longer need the display data."
