#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != "Linux" ] || ! command -v systemctl >/dev/null 2>&1; then
  echo "Controller uninstaller requires Linux with systemd." >&2
  exit 1
fi

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
native_names=$(python3 "${root}/src/inky_bird_frame/native_names.py")
service_prefix="" app_dir="" support_dir="" config_path=""
eval "${native_names}"

if ! sudo -v; then
  echo "Administrator access is required to remove system services." >&2
  exit 1
fi

failures=()
installed() {
  [ "$(systemctl show --property=LoadState --value "$1" 2>/dev/null || true)" != "not-found" ] || \
    [ -e "/etc/systemd/system/$1" ]
}
disable_unit() {
  if ! installed "$1"; then
    return 0
  fi
  if sudo systemctl disable --now "$1"; then
    echo "Stopped and disabled $1."
  else
    failures+=("$1")
  fi
}
stop_unit() {
  if ! installed "$1"; then
    return 0
  fi
  if sudo systemctl stop "$1"; then
    echo "Stopped $1."
  else
    failures+=("$1")
  fi
}

not_stopped() {
  local failed
  for failed in "${failures[@]+"${failures[@]}"}"; do
    [ "${failed}" = "$1" ] && return 0
  done
  return 1
}

for name in refresh generate catalog-publish notifications; do
  disable_unit "${service_prefix}-${name}.timer"
  stop_unit "${service_prefix}-${name}.service"
done
disable_unit "${service_prefix}-controller.service"
disable_unit "${service_prefix}-scheduler.service"

for name in controller scheduler refresh generate catalog-publish notifications; do
  for unit in "${service_prefix}-${name}.service" "${service_prefix}-${name}.timer"; do
    # Keep the file of a unit that may still run so a rerun finds and stops it.
    if not_stopped "${unit}"; then
      continue
    fi
    if [ -f "/etc/systemd/system/${unit}" ]; then
      if sudo rm -f "/etc/systemd/system/${unit}"; then
        echo "Removed /etc/systemd/system/${unit}."
      else
        failures+=("/etc/systemd/system/${unit}")
      fi
    fi
  done
done
if sudo systemctl daemon-reload; then
  echo "Reloaded the systemd unit configuration."
else
  failures+=("systemd unit reload")
fi

if [ "${#failures[@]}" -gt 0 ]; then
  echo "Uninstall did not finish. Could not stop or remove: ${failures[*]}." >&2
  echo "Fix the cause shown above and run the uninstaller again." >&2
  exit 1
fi
echo "Controller services uninstalled."
echo "Intentionally left in place:"
echo "  Application runtime and managed catalog: ${app_dir}"
echo "  Configuration: ${config_path}"
echo "  Support data: ${support_dir}"
echo "  Workspace, state, and catalog directories configured in ${config_path}"
echo "Remove those paths manually if you no longer need the controller data."
