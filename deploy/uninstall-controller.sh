#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
  echo "Controller uninstaller currently supports macOS." >&2
  exit 1
fi
# Printed commands quote what they name, so one can be pasted as it is.
script_command=$(printf '%q' "$0")

# --- system-service mode -----------------------------------------------------
# Options are only those of a system service: without them, the uninstaller
# removes this account's own services as before.
service_account=""
install_root=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --service-account | --install-root)
      if [ "$#" -lt 2 ]; then
        echo "$1 needs a value. No changes made." >&2
        exit 1
      fi
      if [ "$1" = --service-account ]; then
        service_account=$2
      else
        install_root=$2
      fi
      shift 2
      ;;
    *)
      echo "Unknown option: $1. No changes made." >&2
      exit 1
      ;;
  esac
done
if [ -n "${install_root}" ] && [ -z "${service_account}" ]; then
  echo "--install-root needs --service-account. No changes made." >&2
  exit 1
fi
if [ -n "${service_account}" ] && [ "$(id -u)" != 0 ]; then
  echo "Remove system services as root: sudo ${script_command} --service-account NAME. No changes made." >&2
  exit 1
fi
# As root, only a system service: the user uninstaller removes the running
# user's own LaunchAgents, and root has none.
if [ -z "${service_account}" ] && [ "$(id -u)" = 0 ]; then
  for plist in /Library/LaunchDaemons/com.inky-bird*.serve.plist; do
    if [ -f "${plist}" ]; then
      owner=${plist##*/com.}
      owner=${owner%.serve.plist}
      echo "${plist##*/} is a system service of ${owner}. Run the uninstaller again with --service-account ${owner}. No changes made." >&2
      exit 1
    fi
  done
  echo "Run the uninstaller as the account that owns the controller, not as root. No changes made." >&2
  exit 1
fi

# Until the layout is known, nothing has changed.
trap 'echo "Uninstall was stopped. No changes made." >&2; exit 129' HUP
trap 'echo "Uninstall was stopped. No changes made." >&2; exit 130' INT
trap 'echo "Uninstall was stopped. No changes made." >&2; exit 143' TERM
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
service_prefix="" app_dir="" support_dir="" config_path=""
if [ -n "${service_account}" ]; then
  # A system service is named after its account.
  service_prefix=${service_account}
else
  if ! native_names=$(python3 "${root}/src/inky_bird_frame/native_names.py"); then
    echo "Could not find where the services are installed. No changes made." >&2
    exit 1
  fi
  eval "${native_names}"
fi
# A stopped uninstall names what it has not removed yet.
stopped() {
  trap '' HUP INT TERM
  local remaining=() label
  local folder="${HOME}/Library/LaunchAgents"
  if [ -n "${service_account}" ]; then
    folder="/Library/LaunchDaemons"
  fi
  for label in serve refresh generate catalog-publish notifications scheduler controller-cycle; do
    if [ -e "${folder}/com.${service_prefix}.${label}.plist" ]; then
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
if [ -n "${service_account}" ]; then
  # A system service: only this account's own jobs and root's code under
  # ${install_root}/app go; the account and its data stay.
  umask 022
  install_root=${install_root:-/opt/${service_account}}
  system_helper="${root}/src/inky_bird_frame/system_service.py"
  account_uid="" account_group="" account_home="" problem="" unsafe_path=""
  preflight=$(python3 -I "${system_helper}" preflight "${service_account}" "${install_root}" "${root}") ||
    preflight="problem=failed"
  eval "${preflight}"
  case "${problem}" in
    "") ;;
    account-name)
      echo "--service-account must be inky-bird, or inky-bird- followed by lowercase letters and digits. No changes made." >&2
      exit 1
      ;;
    account-missing)
      echo "The account ${service_account} does not exist; create it as docs/installation.md shows. No changes made." >&2
      exit 1
      ;;
    account-root)
      echo "The account ${service_account} has root's user or group ID; use an account of its own. No changes made." >&2
      exit 1
      ;;
    account-admin)
      echo "The account ${service_account} is in an administrator group; remove it from that group. No changes made." >&2
      exit 1
      ;;
    account-shell)
      echo "The account ${service_account} can log in; give it the shell /usr/bin/false. No changes made." >&2
      exit 1
      ;;
    account-home)
      quoted_install_root=$(printf '%q' "${install_root}")
      echo "The home directory of ${service_account} must be ${quoted_install_root}/home. No changes made." >&2
      exit 1
      ;;
    root-path)
      echo "--install-root must be an absolute path without . or .. parts, other than /. No changes made." >&2
      exit 1
      ;;
    root-unsafe)
      quoted_install_root=$(printf '%q' "${install_root}")
      echo "${quoted_install_root}, or a folder above it, can be changed by someone other than root. No changes made." >&2
      exit 1
      ;;
    source-writable)
      quoted_root=$(printf '%q' "${root}")
      echo "${service_account} can change the source checkout ${quoted_root}; use a checkout only root can change. No changes made." >&2
      exit 1
      ;;
    layout-unsafe)
      quoted_unsafe_path=$(printf '%q' "${unsafe_path}")
      echo "${quoted_unsafe_path} must be a folder, not a link: under app owned by root, otherwise by ${service_account}. No changes made." >&2
      exit 1
      ;;
    layout-unreadable)
      quoted_unsafe_path=$(printf '%q' "${unsafe_path}")
      echo "${service_account} cannot read or search ${quoted_unsafe_path}; give its folders mode 0755 (chmod go+rX). No changes made." >&2
      exit 1
      ;;
    *)
      echo "Could not check the service account. No changes made." >&2
      exit 1
      ;;
  esac
  service_prefix=${service_account}
  app_dir="${install_root}/app"
  config_path="${install_root}/config/config.toml"
  quiesce_timeout_seconds=1800
  presence_timeout_seconds=30
  poll_seconds=1
  # The lock holder, which runs as the account, says in its own home that it
  # holds the locks; root only looks whether the file is there.
  ready_dir="${install_root}/home"
  lock_pid=""
  lock_holds=0
  # Start a command in its own process group and record its ID in $1.
  start_background() {
    local name=$1
    shift
    set -m
    "$@" </dev/null &
    printf -v "${name}" '%s' "$!"
    set +m
  }
  # Stop what start_background started, by its group: ask, then force.
  stop_group() {
    local deadline=$((SECONDS + 10))
    kill -TERM -- "-$1" 2>/dev/null || true
    while kill -0 "$1" 2>/dev/null && [ "${SECONDS}" -lt "${deadline}" ]; do
      sleep "${poll_seconds}"
    done
    kill -KILL -- "-$1" 2>/dev/null || true
    wait "$1" 2>/dev/null || true
  }
  # Hold every writer lock, as the account, with the installed version's
  # helper; with no installed version left, there is nothing to hold. Held
  # "again", once the jobs stopped, a failure is no longer a refusal.
  hold_locks() {
    local python="${app_dir}/.venv/bin/python"
    local ready status=0
    lock_holds=$((lock_holds + 1))
    ready="${ready_dir}/.inky-bird-uninstall.$$.${lock_holds}"
    if [ ! -x "${python}" ]; then
      return 0
    fi
    "${account_prefix[@]}" rm -f "${ready}"
    start_background lock_pid "${account_prefix[@]}" "${python}" -I \
      -m inky_bird_frame.upgrade_gates hold-writer-locks "${config_path}" "${ready}" \
      "${quiesce_timeout_seconds}"
    until [ -e "${ready}" ]; do
      if ! kill -0 "${lock_pid}" 2>/dev/null; then
        wait "${lock_pid}" || status=$?
        lock_pid=""
        # Without the ready file nothing is held, whatever the exit status.
        if [ "${1:-}" = again ]; then
          echo "Could not hold the writer locks again; the code was not removed. Run the uninstaller again to finish." >&2
          exit 1
        fi
        case "${status}" in
          0)
            echo "The installed version cannot read its configuration, so the uninstaller cannot check for running work. Fix the configuration so the installed version can read it, then run the uninstaller again. No changes made." >&2
            exit 1
            ;;
          3)
            echo "A plate is still being made. Run the uninstaller again after it finishes. No changes made." >&2
            exit 3
            ;;
          4)
            echo "Other work is still running. Run the uninstaller again after it finishes. No changes made." >&2
            exit 3
            ;;
          *)
            echo "Could not check whether a plate is being made. No changes made." >&2
            exit 1
            ;;
        esac
      fi
      sleep "${poll_seconds}"
    done
  }
  release_locks() {
    if [ -n "${lock_pid}" ]; then
      stop_group "${lock_pid}"
      lock_pid=""
      "${account_prefix[@]}" rm -f "${ready_dir}/.inky-bird-uninstall.$$.${lock_holds}" || true
    fi
  }
  # Wait until no process of the account runs, other than the lock holder.
  account_processes_gone() {
    local deadline=$((SECONDS + presence_timeout_seconds))
    local listed pid others status
    while true; do
      status=0
      listed=$(pgrep -u "${account_uid}") || status=$?
      if [ "${status}" -gt 1 ]; then
        return 1
      fi
      others=""
      for pid in ${listed}; do
        if [ "${pid}" != "${lock_pid}" ]; then
          others="${others} ${pid}"
        fi
      done
      if [ -z "${others}" ]; then
        return 0
      fi
      if [ "${SECONDS}" -ge "${deadline}" ]; then
        return 1
      fi
      sleep "${poll_seconds}"
    done
  }
  finish_system() {
    local status=$?
    # A stop now would end it before the locks are released.
    trap '' HUP INT TERM
    trap - EXIT
    release_locks
    exit "${status}"
  }
  account_prefix=(python3 -I "${system_helper}" run-as "${service_account}" --
    env -i "PATH=/usr/bin:/bin:/usr/sbin:/sbin" "HOME=${account_home}" "LANG=en_US.UTF-8"
    "INKY_BIRD_ENV_FILE=${install_root}/config/private.env")
  daemons_dir="/Library/LaunchDaemons"
  labels=(serve refresh generate catalog-publish notifications scheduler)
  # Only this account's own LaunchDaemons: each runs as the account it is named after.
  for label in "${labels[@]}"; do
    plist="${daemons_dir}/com.${service_prefix}.${label}.plist"
    if [ -f "${plist}" ] && [ "$(python3 -I -c '
import plistlib, sys
try:
    with open(sys.argv[1], "rb") as handle:
        print(plistlib.load(handle).get("UserName", ""))
except Exception:
    print("")
' "${plist}")" != "${service_account}" ]; then
      echo "com.${service_prefix}.${label} does not run as ${service_account}; it is not this installation's. No changes made." >&2
      exit 1
    fi
    # The user who runs sudo may have the same names as LaunchAgents.
    if [ -n "${SUDO_UID:-}" ] && \
        launchctl print "gui/${SUDO_UID}/com.${service_prefix}.${label}" >/dev/null 2>&1; then
      agent_owner=${SUDO_USER:-${SUDO_UID}}
      echo "com.${service_prefix}.${label} is a LaunchAgent of ${agent_owner}; remove it with deploy/uninstall-controller.sh run as that user. No changes made." >&2
      exit 1
    fi
  done
  trap finish_system EXIT
  failures=()
  # No writer is mid-write while its jobs stop; launchd ends one still
  # running after its exit timeout.
  hold_locks
  for label in "${labels[@]}"; do
    daemon="com.${service_prefix}.${label}"
    if launchctl print "system/${daemon}" >/dev/null 2>&1; then
      if launchctl bootout "system/${daemon}"; then
        echo "Booted out LaunchDaemon: ${daemon}"
      else
        failures+=("${daemon}")
      fi
    fi
  done
  release_locks
  if [ "${#failures[@]}" -eq 0 ] && ! account_processes_gone; then
    failures+=("the processes of ${service_account}")
  fi
  if [ "${#failures[@]}" -gt 0 ]; then
    echo "Uninstall did not finish. Could not stop or remove: ${failures[*]}." >&2
    echo "Fix the cause shown above and run the uninstaller again." >&2
    exit 1
  fi
  # The code goes only with every writer lock held.
  hold_locks again
  for label in "${labels[@]}"; do
    plist="${daemons_dir}/com.${service_prefix}.${label}.plist"
    if [ -f "${plist}" ]; then
      if rm -f "${plist}"; then
        echo "Removed plist: ${plist}"
      else
        failures+=("${plist}")
      fi
    fi
  done
  if [ "${#failures[@]}" -eq 0 ] && { [ -e "${app_dir}" ] || [ -L "${app_dir}" ]; }; then
    if python3 -I "${system_helper}" remove-code "${service_account}" "${install_root}"; then
      quoted_app_dir=$(printf '%q' "${app_dir}")
      echo "Removed the application code: ${quoted_app_dir}"
    else
      failures+=("${app_dir}")
    fi
  fi
  release_locks
else
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

fi

if [ "${#failures[@]}" -gt 0 ]; then
  echo "Uninstall did not finish. Could not remove: ${failures[*]}." >&2
  echo "Fix the cause shown above and run the uninstaller again." >&2
  exit 1
fi
jobs_kind=LaunchAgents
if [ -n "${service_account}" ]; then
  jobs_kind=LaunchDaemons
fi
echo "Controller ${jobs_kind} uninstalled."
echo "Intentionally left in place:"
if [ -n "${service_account}" ]; then
  quoted_install_root=$(printf '%q' "${install_root}")
  echo "  Configuration and private environment file: ${quoted_install_root}/config"
  echo "  Home, state and logs: ${quoted_install_root}/home, ${quoted_install_root}/state, ${quoted_install_root}/logs"
  quoted_config_path=$(printf '%q' "${config_path}")
  echo "  Workspace, state, and catalog directories configured in ${quoted_config_path}"
  echo "  The account ${service_account}"
  quoted_root=$(printf '%q' "${install_root}")
  # The group goes too only when it is the account's own: named after it, with
  # no other member and no other account in it.
  if python3 -I "${system_helper}" own-group "${service_account}" "${install_root}"; then
    echo "Once you no longer need the controller data, remove them with: sudo rm -R ${quoted_root} && sudo dscl . -delete /Users/${service_account} && sudo dscl . -delete /Groups/${service_account}"
  else
    quoted_group=$(printf '%q' "${account_group}")
    echo "Once you no longer need the controller data, remove them with: sudo rm -R ${quoted_root} && sudo dscl . -delete /Users/${service_account}"
    echo "The group ${quoted_group} is not the account's own, so it is left alone."
  fi
  exit 0
fi
quoted_app_dir=$(printf '%q' "${app_dir}")
echo "  Application runtime and managed catalog: ${quoted_app_dir}"
quoted_config_path=$(printf '%q' "${config_path}")
echo "  Configuration: ${quoted_config_path}"
echo "  Logs and support data: ${support_dir}"
echo "  Workspace, state, and catalog directories configured in ${quoted_config_path}"
echo "Remove those paths manually if you no longer need the controller data."
