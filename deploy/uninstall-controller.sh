#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
  echo "Controller uninstaller currently supports macOS." >&2
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
  local remaining=() label
  for label in serve refresh generate catalog-publish notifications scheduler controller-cycle; do
    if [ -e "${HOME}/Library/LaunchAgents/com.${service_prefix}.${label}.plist" ]; then
      remaining+=("com.${service_prefix}.${label}")
    fi
  done
  echo "Uninstall was stopped before it finished; still installed: ${remaining[*]-none}." >&2
  echo "Run the uninstaller again to finish." >&2
  exit "$1"
}
trap 'stopped 129' HUP
trap 'stopped 130' INT
trap 'stopped 143' TERM
agents_dir="${HOME}/Library/LaunchAgents"
uid=$(id -u)
failures=()

# controller-cycle is the legacy single-agent layout replaced by the labels below.
for label in serve refresh generate catalog-publish notifications scheduler controller-cycle; do
  agent="com.${service_prefix}.${label}"
  plist="${agents_dir}/${agent}.plist"
  if launchctl print "gui/${uid}/${agent}" >/dev/null 2>&1; then
    if ! launchctl bootout "gui/${uid}/${agent}"; then
      # Keep the plist so the agent can be found and removed on a rerun.
      failures+=("${agent}")
      continue
    fi
    echo "Booted out LaunchAgent: ${agent}"
  else
    echo "LaunchAgent is not loaded: ${agent}"
  fi
  if [ -f "${plist}" ]; then
    if rm -f "${plist}"; then
      echo "Removed plist: ${plist}"
    else
      failures+=("${plist}")
    fi
  else
    echo "Plist is not installed: ${plist}"
  fi
done

if [ "${#failures[@]}" -gt 0 ]; then
  echo "Uninstall did not finish. Could not remove: ${failures[*]}." >&2
  echo "Fix the cause shown above and run the uninstaller again." >&2
  exit 1
fi
echo "Controller LaunchAgents uninstalled."
echo "Intentionally left in place:"
echo "  Application runtime and managed catalog: ${app_dir}"
echo "  Configuration: ${config_path}"
echo "  Logs and support data: ${support_dir}"
echo "  Workspace, state, and catalog directories configured in ${config_path}"
echo "Remove those paths manually if you no longer need the controller data."
