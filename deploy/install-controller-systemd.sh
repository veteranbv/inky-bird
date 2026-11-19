#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != "Linux" ] || ! command -v systemctl >/dev/null 2>&1; then
  echo "Controller installer requires Linux with systemd." >&2
  exit 1
fi
# Printed commands quote what they name, so one can be pasted as it is.
script_command=$(printf '%q' "$0")
unit_root="/etc/systemd/system"
# The first line of each unit a system-service installation renders.
system_marker="# Inky Bird system service"
system_requested=false
for argument in "$@"; do
  if [ "${argument}" = --service-account ]; then
    system_requested=true
  fi
done
current_uid=$(id -u)
if [ "${current_uid}" = 0 ] && [ "${system_requested}" = false ]; then
  for unit in "${unit_root}"/inky-bird*-controller.service; do
    if [ -f "${unit}" ] && [ "$(head -n 1 "${unit}")" = "${system_marker}" ]; then
      owner=$(sed -n 's/^User=//p' "${unit}" | tail -n 1)
      echo "${unit##*/} is a system service of ${owner}. Run the installer again with --service-account ${owner}. No changes made." >&2
      exit 1
    fi
  done
  echo "Run the installer as the account that will own the controller, not as root. No changes made." >&2
  exit 1
fi
if [ "${system_requested}" = true ] && [ "${current_uid}" != 0 ]; then
  echo "Install system services as root: sudo ${script_command} --service-account NAME. No changes made." >&2
  exit 1
fi

# Print the real path of $1 if it is safe to run: an absolute path whose real
# path is an executable regular file, with every directory from / down to it
# owned by this account or root and writable by no one else (or, for a shared
# directory such as /tmp, sticky, so no one else can replace what is in it).
# The installer runs that real path, so no link can be swapped in afterwards.
stopped_hook_real_path() {
  python3 -I -c '
import os, stat, sys
path = sys.argv[1]
def owned(info):
    return info.st_uid in (os.getuid(), 0)
def safe_directory(info):
    shared = info.st_mode & stat.S_ISVTX
    return stat.S_ISDIR(info.st_mode) and owned(info) and (shared or not info.st_mode & 0o022)
try:
    if not os.path.isabs(path):
        sys.exit(1)
    real = os.path.realpath(path)
    hook = os.lstat(real)
    if not (stat.S_ISREG(hook.st_mode) and owned(hook) and not hook.st_mode & 0o022):
        sys.exit(1)
    if not os.access(real, os.X_OK):
        sys.exit(1)
    directory = os.path.dirname(real)
    while True:
        if not safe_directory(os.lstat(directory)):
            sys.exit(1)
        if directory == "/":
            break
        directory = os.path.dirname(directory)
except OSError:
    sys.exit(1)
print(real)
' "$1"
}

stopped_hook=""
stopped_hook_timeout=0
service_account=""
install_root=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --stopped-hook | --stopped-hook-timeout | --service-account | --install-root)
      if [ "$#" -lt 2 ]; then
        echo "$1 needs a value. No changes made." >&2
        exit 1
      fi
      case "$1" in
        --stopped-hook) stopped_hook=$2 ;;
        --stopped-hook-timeout) stopped_hook_timeout=$2 ;;
        --service-account) service_account=$2 ;;
        *) install_root=$2 ;;
      esac
      shift 2
      ;;
    *)
      echo "Unknown option: $1. No changes made." >&2
      exit 1
      ;;
  esac
done
case "${stopped_hook_timeout}" in
  "" | *[!0-9]*)
    echo "--stopped-hook-timeout must be a whole number of seconds. No changes made." >&2
    exit 1
    ;;
esac
stopped_hook_timeout=$((10#${stopped_hook_timeout}))
if [ -n "${stopped_hook}" ]; then
  if ! stopped_hook=$(stopped_hook_real_path "${stopped_hook}"); then
    echo "--stopped-hook must name, by absolute path, an executable file that only you or root can change, in directories only you or root can change. No changes made." >&2
    exit 1
  fi
fi
if [ -z "${stopped_hook}" ] && [ "${stopped_hook_timeout}" -gt 0 ]; then
  echo "--stopped-hook-timeout needs --stopped-hook. No changes made." >&2
  exit 1
fi
if [ -n "${install_root}" ] && [ -z "${service_account}" ]; then
  echo "--install-root needs --service-account. No changes made." >&2
  exit 1
fi

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# The managed directory's copy of this script reinstalls the active runtime.
# An environment moved out of the single-directory layout has no source of its
# own; it still imports ${root}/src, so reinstall from there.
# A source checkout used as the managed directory installs what it holds.
if [ -L "${root}/.venv" ] && [ -f "${root}/deploy/.managed-copy" ]; then
  active_root=$(cd -P "${root}/.venv/.." 2>/dev/null && pwd || true)
  if [ -n "${active_root}" ] && [ -d "${active_root}/src" ]; then
    root=${active_root}
  fi
fi
service_prefix="" app_dir="" support_dir="" config_path=""
account_uid="" account_gid="" account_group="" account_home="" problem="" unsafe_path=""
give_directories=""
if [ -z "${service_account}" ]; then
  native_names=$(python3 "${root}/src/inky_bird_frame/native_names.py")
  eval "${native_names}"
else
  # A system service: the account names the services and owns the data; the
  # code under ${install_root}/app is root's alone.
  umask 022
  install_root=${install_root:-/srv/${service_account}}
  system_helper="${root}/src/inky_bird_frame/system_service.py"
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
      echo "The account ${service_account} can log in; give it the shell /usr/sbin/nologin. No changes made." >&2
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
  support_dir="${install_root}/config"
  config_path="${support_dir}/config.toml"
  environment_file="${support_dir}/private.env"
fi
export INKY_BIRD_SERVICE_PREFIX="${service_prefix}"
python_version=${INKY_BIRD_PYTHON_VERSION:-3.11}
quiesce_timeout_seconds=1800
poll_seconds=1
health_timeout_seconds=90
uv_bin=${UV_BIN:-}
if [ -z "${uv_bin}" ]; then
  uv_bin=$(command -v uv || true)
fi
# Each runtime holds one source copy and the venv built from it;
# ${app_dir}/.venv links to the active one.
runtimes_dir="${app_dir}/.runtimes"
# setup --yes from the installed CLI runs the active runtime's own copy of this
# script. A runtime holds no catalog; that stays in the managed directory.
catalog_root=${root}
if [ "$(cd -P "${root}/.." && pwd)" = "$(cd -P "${runtimes_dir}" 2>/dev/null && pwd || true)" ]; then
  catalog_root=${app_dir}
fi
if [ -n "${service_account}" ]; then
  user=${service_account}
else
  user=$(id -un)
fi
managed_units=()
for name in controller scheduler refresh generate catalog-publish notifications; do
  managed_units+=("${service_prefix}-${name}.service" "${service_prefix}-${name}.timer")
done

# A system service's installer runs as root: it changes the code and the
# units itself, and runs the application as the account, with only the
# environment given here.
account_prefix=()
copy_options=()
presence_timeout_seconds=30
if [ -n "${service_account}" ]; then
  # A copy is root's, whatever owner the checkout's files have; share_copy
  # sets its modes.
  copy_options=(--no-owner --no-group)
  account_prefix=(setpriv "--reuid=${account_uid}" "--regid=${account_gid}" --init-groups --
    env -i "PATH=/usr/local/bin:/usr/bin:/bin" "HOME=${account_home}" "LANG=C.UTF-8"
    "INKY_BIRD_ENV_FILE=${environment_file}")
fi
admin() {
  if [ -n "${service_account}" ]; then
    "$@"
  else
    sudo "$@"
  fi
}
admin_ready() {
  [ -n "${service_account}" ] || sudo -v
}
as_account() {
  ${account_prefix[@]+"${account_prefix[@]}"} "$@"
}
# A folder mktemp made (mode 0700) that the account must read: the code stays
# root's alone to change, as everything else under app is (0755).
share_with_account() {
  [ -z "${service_account}" ] || chmod 0755 "$1"
}
# Whether the account can reach $1 and read everything in it.
account_can_read() {
  [ -z "${service_account}" ] ||
    python3 -I "${system_helper}" readable "${service_account}" "${install_root}" "$1"
}
# Whether root may delete, move or replace $2 inside $1. For a system service
# the path must lie under the install root through folders the account cannot
# change, so it cannot lead out between this check and its use.
contained() {
  [ -z "${service_account}" ] ||
    python3 -I "${system_helper}" contained "${service_account}" "${install_root}" "$1" "$2"
}
# Wait up to presence_timeout_seconds until no process of the service account
# runs, other than the writer lock holder this run started.
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
      if [ "${pid}" != "${writer_lock_pid}" ]; then
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

if [ ! -f "${config_path}" ]; then
  quoted_config_path=$(printf '%q' "${config_path}")
  echo "Controller configuration is missing: ${quoted_config_path}" >&2
  exit 1
fi
if [ -z "${uv_bin}" ] || [ ! -x "${uv_bin}" ]; then
  echo "uv is not executable. Install uv or set UV_BIN." >&2
  exit 1
fi
for name in controller scheduler refresh generate catalog-publish notifications; do
  unit="${unit_root}/${service_prefix}-${name}.service"
  if [ -f "${unit}" ]; then
    owner=$(sed -n 's/^User=//p' "${unit}" | tail -n 1)
    owner=${owner:-root}
    if [ -n "${service_account}" ] && [ "$(head -n 1 "${unit}")" != "${system_marker}" ]; then
      echo "${service_prefix}-${name}.service was installed by the user installer for ${owner}; this installer does not move it. Remove it with deploy/uninstall-controller-systemd.sh run as ${owner}, then run this again. No changes made." >&2
      exit 1
    fi
    if [ "${owner}" != "${user}" ]; then
      if [ "$(head -n 1 "${unit}")" = "${system_marker}" ]; then
        echo "${service_prefix}-${name}.service is a system service of ${owner}. Upgrade it as root: sudo ${script_command} --service-account ${owner}. No changes made." >&2
        exit 1
      fi
      echo "${service_prefix}-${name}.service runs as ${owner}. Run the installer as ${owner}. No changes made." >&2
      exit 1
    fi
  fi
done
if ! admin_ready; then
  echo "Administrator access is required to install system services." >&2
  exit 1
fi

work_dir=""
ready_dir=""
unit_dir=""
unit_backup_dir=""
created_runtimes_dir=false
new_runtime=""
previous_runtime=""
legacy_runtime=""
venv_before=""
units_recorded=false
runtime_activated=false
units_replaced=false
installation_complete=false
writer_lock_pid=""
writer_lock_holds=0
writer_locks_unavailable=false
bounded_pid=""
bounded_timed_out=false
stopped_hook_ran=false
stopped_hook_state_dir=""
enabled_before=()
active_before=()
stopped_units=()
started_units=()
stuck_units=()
venv_restored=true
leftovers=()
new_runtime_linked=false

# mktemp -d into the variable named $1. A stop signal that arrives after the
# directory exists but before its name is recorded waits until it is, so
# cleanup can still remove it.
make_temp_dir() {
  local created=""
  stop_status=""
  trap 'stop_status=129' HUP
  trap 'stop_status=130' INT
  trap 'stop_status=143' TERM
  created=$(mktemp -d ${2:+"$2"}) || created=""
  if [ -n "${created}" ]; then
    printf -v "$1" '%s' "${created}"
  fi
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  if [ -n "${stop_status}" ]; then
    exit "${stop_status}"
  fi
  [ -n "${created}" ]
}

# A system service's copy is root's and readable by everyone, whatever modes
# the checkout's files have (a root umask of 077 makes them 0600): folders
# 0755, files 0644. Not rsync --chmod: macOS's openrsync accepts it and
# ignores it. find follows no links.
share_copy() {
  if [ -n "${service_account}" ]; then
    find "$1" -type d -exec chmod 0755 {} +
    find "$1" -type f -exec chmod 0644 {} +
  fi
}
# Copy what building, and later reinstalling, a runtime needs from ${root}.
stage_runtime_source() {
  local target=$1
  rsync -a --delete ${copy_options[@]+"${copy_options[@]}"} "${root}/src/" "${target}/src/"
  share_copy "${target}/src"
  mkdir -p "${target}/deploy" "${target}/docs"
  install -m 0755 "${root}/deploy/install-controller-systemd.sh" "${target}/deploy/"
  for file in pyproject.toml uv.lock README.md LICENSE docs/browser-access.md; do
    install -m 0644 "${root}/${file}" "${target}/${file}"
  done
}

contains() {
  local target=$1
  local item
  shift
  for item in "$@"; do
    if [ "${item}" = "${target}" ]; then
      return 0
    fi
  done
  return 1
}

# The controller and scheduler run continuously and timers keep their own
# state; the services the timers start are jobs that come and go.
long_running() {
  case "$1" in
    *.timer | *-controller.service | *-scheduler.service) return 0 ;;
  esac
  return 1
}

# Whether anything of a unit is running. A oneshot service is "activating"
# while it runs and never "active", so is-active alone misses it.
running() {
  case "$(systemctl show --property=ActiveState --value "$1" 2>/dev/null || true)" in
    active | activating | deactivating | reloading) return 0 ;;
  esac
  return 1
}

# Remove the work folders a power loss left where this installer keeps them,
# from before the current boot: no installer or job can still use them. Only
# with the writer locks held, or with no installed version to write; the
# helper removes only this user's own folders and follows no link.
remove_stale_work() {
  local parent=${support_dir}
  if [ -n "${service_account}" ]; then
    parent=${install_root}
  fi
  if [ "${writer_locks_unavailable}" = false ]; then
    "${new_runtime}/.venv/bin/python" -I -m inky_bird_frame.upgrade_gates remove-stale-work \
      "${parent}"
  fi
}
# Hold every lock a writer takes, so nothing is mid-write while services
# stop: the installed version's Python runs the new version's helper, so the
# installed code finds the state directory. It waits up to
# quiesce_timeout_seconds and says what it waits for. If the installed code
# cannot be loaded or cannot read the config, it says so and holds nothing, so
# a broken or older install can still be upgraded or repaired. $1 is true once
# this run has stopped something.
hold_writer_locks() {
  local python="${app_dir}/.venv/bin/python"
  local ready
  local status=0
  # The helper gives up at the timeout; one that never answers is stopped.
  local deadline=$((SECONDS + quiesce_timeout_seconds + 60))
  writer_lock_holds=$((writer_lock_holds + 1))
  ready="${ready_dir}/writer-locks-held.${writer_lock_holds}"
  if [ ! -x "${python}" ] || [ "${writer_locks_unavailable}" = true ]; then
    return 0
  fi
  start_background writer_lock_pid ${account_prefix[@]+"${account_prefix[@]}"} "${python}" -I \
    "${new_runtime}/src/inky_bird_frame/upgrade_gates.py" hold-writer-locks \
    "${config_path}" "${ready}" "${quiesce_timeout_seconds}"
  until [ -e "${ready}" ]; do
    if [ "${SECONDS}" -ge "${deadline}" ]; then
      stop_group "${writer_lock_pid}"
      writer_lock_pid=""
      status=1
    elif ! kill -0 "${writer_lock_pid}" 2>/dev/null; then
      wait "${writer_lock_pid}" || status=$?
      writer_lock_pid=""
    fi
    if [ -z "${writer_lock_pid}" ]; then
      if [ "${status}" -eq 0 ]; then
        # The installed code could not be loaded; it said so once.
        writer_locks_unavailable=true
        return 0
      fi
      if [ "$1" = true ]; then
        if [ "${status}" -eq 3 ] || [ "${status}" -eq 4 ]; then
          echo "Work that started during the upgrade did not finish in time; the upgrade stopped before switching to the new version." >&2
          exit 3
        fi
        echo "Could not hold the writer locks again; the upgrade stopped before switching to the new version." >&2
        exit 1
      fi
      if [ "${status}" -eq 3 ]; then
        echo "A plate is still being made. Run the upgrade again after it finishes. No changes made." >&2
        exit 3
      fi
      if [ "${status}" -eq 4 ]; then
        echo "Other work is still running. Run the upgrade again after it finishes. No changes made." >&2
        exit 3
      fi
      echo "Could not check whether a plate is being made. No changes made." >&2
      exit 1
    fi
    sleep "${poll_seconds}"
  done
}

release_writer_locks() {
  if [ -n "${writer_lock_pid}" ]; then
    stop_group "${writer_lock_pid}"
    writer_lock_pid=""
  fi
}

# Start a command in the background, in a process group of its own, and
# record its process ID in the variable named $1. A stop signal that arrives
# before the ID is recorded waits until it is, so cleanup can still stop it.
start_background() {
  local name=$1
  shift
  stop_status=""
  trap 'stop_status=129' HUP
  trap 'stop_status=130' INT
  trap 'stop_status=143' TERM
  set -m
  "$@" </dev/null &
  printf -v "${name}" '%s' "$!"
  set +m
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  if [ -n "${stop_status}" ]; then
    exit "${stop_status}"
  fi
}

# Run a command in the background, in a process group of its own, so a stop
# signal is handled while it runs and everything it started can be stopped,
# and wait for it. With a limit of $1 seconds (0: none), stop it once the
# limit passes and return 124 with bounded_timed_out=true.
run_bounded() {
  local limit=$1
  local deadline=$((SECONDS + $1))
  local status=0
  shift
  bounded_timed_out=false
  start_background bounded_pid "$@"
  if [ "${limit}" -gt 0 ]; then
    while kill -0 "${bounded_pid}" 2>/dev/null; do
      if [ "${SECONDS}" -ge "${deadline}" ]; then
        stop_bounded
        bounded_timed_out=true
        return 124
      fi
      sleep "${poll_seconds}"
    done
  fi
  wait "${bounded_pid}" || status=$?
  bounded_pid=""
  return "${status}"
}

# Stop a process start_background started, with its whole process group,
# and wait until it has gone: ask, then after at most 10 seconds force. It
# leads its group, so only the group is signalled: the ID alone may already
# belong to another process once the first has exited.
stop_group() {
  local deadline=$((SECONDS + 10))
  kill -TERM -- "-$1" 2>/dev/null || true
  while kill -0 "$1" 2>/dev/null && [ "${SECONDS}" -lt "${deadline}" ]; do
    sleep "${poll_seconds}"
  done
  # Whatever of the group is left, the command itself or what it started.
  kill -KILL -- "-$1" 2>/dev/null || true
  wait "$1" 2>/dev/null || true
}

# Stop what run_bounded started.
stop_bounded() {
  if [ -n "${bounded_pid}" ]; then
    stop_group "${bounded_pid}"
    bounded_pid=""
  fi
}

# Run the owner's --stopped-hook, once: services are stopped, the writer
# locks are held, and the previous version is still the active one. It gets
# only names: the runtimes, the config path and the state directory.
run_stopped_hook() {
  local previous=""
  local status=0
  if [ -z "${stopped_hook}" ] || [ "${stopped_hook_ran}" = true ]; then
    return 0
  fi
  stopped_hook_ran=true
  if [ "$(stopped_hook_real_path "${stopped_hook}" || true)" != "${stopped_hook}" ]; then
    echo "The stopped-boundary hook is no longer safe to run; the upgrade did not switch to the new version." >&2
    exit 1
  fi
  if [ -L "${app_dir}/.venv" ]; then
    previous=$(cd -P "${app_dir}/.venv/.." 2>/dev/null && pwd || true)
  elif [ -e "${app_dir}/.venv" ]; then
    previous=${app_dir}
  fi
  quoted_stopped_hook=$(printf '%q' "${stopped_hook}")
  echo "Running the stopped-boundary hook: ${quoted_stopped_hook}" >&2
  run_bounded "${stopped_hook_timeout}" env -i "PATH=${PATH}" "HOME=${HOME}" \
    "INKY_BIRD_HOOK_PREVIOUS_RUNTIME=${previous}" \
    "INKY_BIRD_HOOK_NEW_RUNTIME=${new_runtime}" \
    "INKY_BIRD_HOOK_CONFIG=${config_path}" \
    "INKY_BIRD_HOOK_STATE_DIR=${stopped_hook_state_dir}" \
    "${stopped_hook}" || status=$?
  if [ "${bounded_timed_out}" = true ]; then
    echo "The stopped-boundary hook did not finish within ${stopped_hook_timeout} seconds and was stopped; the upgrade did not switch to the new version." >&2
    exit 1
  fi
  if [ "${status}" -ne 0 ]; then
    echo "The stopped-boundary hook failed with exit status ${status}; the upgrade did not switch to the new version." >&2
    exit 1
  fi
}

# Loaded is not working: the new web service must answer its health check
# from the new runtime, on this machine. The check imports the package as the
# service does, editable or not, so both name the same runtime.
check_new_serve() {
  local environment=()
  if [ -n "${service_account}" ]; then
    environment=("${environment_file}")
  fi
  if ! run_bounded "$((health_timeout_seconds + 15))" \
      ${account_prefix[@]+"${account_prefix[@]}"} "${new_runtime}/.venv/bin/python" -I \
      -m inky_bird_frame.upgrade_gates check-serve "${config_path}" \
      "${health_timeout_seconds}" ${environment[@]+"${environment[@]}"}; then
    echo "The new version started, but its web service failed its health check." >&2
    exit 1
  fi
}


# Ask a long-running service to stop while every writer lock is held, so it is
# between writes when it is signalled. Its own shutdown can need one of those
# locks (the scheduler records the end of the job it stops), so do not wait
# for it to exit here: wait_until_stopped does, once they are released.
request_stop() {
  local unit=$1
  local deadline=$((SECONDS + 30))
  stopped_units+=("${unit}")
  admin systemctl stop --no-block "${unit}"
  while [ "$(systemctl show --property=ActiveState --value "${unit}" 2>/dev/null || true)" = active ]; do
    if [ "${SECONDS}" -ge "${deadline}" ]; then
      echo "${unit} did not begin to stop." >&2
      exit 1
    fi
    sleep "${poll_seconds}"
  done
}

# Wait until none of the named services runs, up to ${deadline}.
wait_until_stopped() {
  local name
  local service
  local state
  for name in "$@"; do
    service="${service_prefix}-${name}.service"
    while true; do
      state=$(systemctl show --property=ActiveState --value "${service}" 2>/dev/null || true)
      case "${state}" in
        active | activating | deactivating | reloading) ;;
        *) break ;;
      esac
      if ((SECONDS >= deadline)); then
        echo "Timed out waiting for ${service} to finish." >&2
        exit 1
      fi
      sleep 2
    done
  done
}

# Remember every managed unit's file and state before anything changes.
record_units() {
  local unit
  for unit in "${managed_units[@]}"; do
    if [ -f "${unit_root}/${unit}" ]; then
      cp "${unit_root}/${unit}" "${unit_backup_dir}/${unit}"
    fi
    if systemctl is-enabled --quiet "${unit}"; then
      enabled_before+=("${unit}")
    fi
    if long_running "${unit}" && systemctl is-active --quiet "${unit}"; then
      active_before+=("${unit}")
    fi
  done
  units_recorded=true
}

# Moves the directory $1 to $2, then renames the link $3 to $1; if that rename
# fails, $1 moves back.
swap_in_link='import os, sys
os.rename(sys.argv[1], sys.argv[2])
try:
    os.replace(sys.argv[3], sys.argv[1])
except OSError:
    os.rename(sys.argv[2], sys.argv[1])
    raise'

replace_venv_link() {
  contained "${app_dir}" "${app_dir}/.venv.next" && contained "${app_dir}" "${app_dir}/.venv" && \
    rm -f "${app_dir}/.venv.next" && ln -s "$1" "${app_dir}/.venv.next" && \
    python3 -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' \
      "${app_dir}/.venv.next" "${app_dir}/.venv"
}

# Point ${app_dir}/.venv, the path every unit runs, at the new runtime. The
# single-directory layout's venv moves into a runtime of its own; its source
# stays at ${app_dir}/src, which that venv imports.
activate_runtime() {
  runtime_activated=true
  if [ -L "${app_dir}/.venv" ]; then
    venv_before="link:$(readlink "${app_dir}/.venv")"
    previous_runtime=$(cd -P "${app_dir}/.venv/.." 2>/dev/null && pwd || true)
  elif [ -e "${app_dir}/.venv" ]; then
    venv_before=directory
    make_temp_dir legacy_runtime "${runtimes_dir}/runtime.XXXXXX"
    share_with_account "${legacy_runtime}"
    previous_runtime=${legacy_runtime}
    contained "${runtimes_dir}" "${legacy_runtime}/.venv"
    # Move the venv aside and link the new runtime in one step, so a power loss
    # cannot leave the units with no venv at all.
    contained "${app_dir}" "${app_dir}/.venv.next" && contained "${app_dir}" "${app_dir}/.venv" && \
      rm -f "${app_dir}/.venv.next" && ln -s "${new_runtime}/.venv" "${app_dir}/.venv.next" && \
      python3 -c "${swap_in_link}" "${app_dir}/.venv" "${legacy_runtime}/.venv" "${app_dir}/.venv.next"
    return
  else
    venv_before=absent
  fi
  replace_venv_link "${new_runtime}/.venv"
}

# Put ${app_dir}/.venv back exactly as it was before this run. Fails only if
# it is not back; a leftover that is not in use is named instead.
restore_venv() {
  case "${venv_before}" in
    link:*)
      if ! replace_venv_link "${venv_before#link:}"; then
        remove_next_link
        return 1
      fi
      ;;
    directory)
      if [ -n "${legacy_runtime}" ] && [ -d "${legacy_runtime}/.venv" ]; then
        if [ -L "${app_dir}/.venv" ]; then
          { contained "${app_dir}" "${app_dir}/.venv" && rm -f "${app_dir}/.venv"; } || return 1
        fi
        if ! { contained "${app_dir}" "${app_dir}/.venv" && mv "${legacy_runtime}/.venv" "${app_dir}/.venv"; }; then
          # Leave the services the new version rather than nothing at all.
          replace_venv_link "${new_runtime}/.venv"
          return 1
        fi
      fi
      if [ -n "${legacy_runtime}" ] && ! { contained "${runtimes_dir}" "${legacy_runtime}" &&
          rmdir "${legacy_runtime}"; }; then
        leftovers+=("${legacy_runtime}")
      fi
      ;;
    *)
      # Nothing ran before this run, so nothing uses the link it made.
      if ! { contained "${app_dir}" "${app_dir}/.venv" && rm -f "${app_dir}/.venv"; }; then
        leftovers+=("${app_dir}/.venv" "${new_runtime}")
        new_runtime_linked=true
      fi
      ;;
  esac
  remove_next_link
}

remove_next_link() {
  if [ -e "${app_dir}/.venv.next" ] || [ -L "${app_dir}/.venv.next" ]; then
    { contained "${app_dir}" "${app_dir}/.venv.next" && rm -f "${app_dir}/.venv.next"; } ||
      leftovers+=("${app_dir}/.venv.next")
  fi
}

# The previous version cannot run from ${app_dir}/.venv any more: restart what
# still runs it so every service uses the version that is there, and say so.
restart_on_new_runtime() {
  local unit
  local restarted=()
  for unit in ${active_before[@]+"${active_before[@]}"}; do
    if [ -e "${unit_root}/${unit}" ] && systemctl is-active --quiet "${unit}" && \
        ! contains "${unit}" ${started_units[@]+"${started_units[@]}"}; then
      if admin systemctl restart "${unit}"; then
        restarted+=("${unit}")
      else
        stuck_units+=("${unit}")
      fi
    fi
  done
  if [ "${#restarted[@]}" -gt 0 ]; then
    echo "These services were restarted on the new version: ${restarted[*]}." >&2
  fi
}

# Return every managed unit to the file and state recorded before the run.
restore_units() {
  local unit
  local changed=false
  local startable
  if [ "${units_replaced}" = true ]; then
    for unit in "${managed_units[@]}"; do
      if [ -f "${unit_backup_dir}/${unit}" ]; then
        if ! cmp -s "${unit_backup_dir}/${unit}" "${unit_root}/${unit}"; then
          changed=true
          admin install -m 0644 "${unit_backup_dir}/${unit}" "${unit_root}/${unit}" || true
        fi
      elif [ -e "${unit_root}/${unit}" ]; then
        changed=true
        if { running "${unit}" || systemctl is-enabled --quiet "${unit}"; } && \
            ! admin systemctl disable --now "${unit}"; then
          # Keep its file so a rerun or the uninstaller can still find and stop it.
          stuck_units+=("${unit}")
          continue
        fi
        admin rm -f "${unit_root}/${unit}" || true
      fi
    done
    if [ "${changed}" = true ]; then
      admin systemctl daemon-reload || true
    fi
  fi
  # A service started after the switch runs the new version; stop it before
  # switching back so it starts again below on the previous one.
  if [ "${runtime_activated}" = true ]; then
    for unit in "${managed_units[@]}"; do
      case "${unit}" in
        *.service)
          if [ -e "${unit_root}/${unit}" ] && running "${unit}" && \
              { ! long_running "${unit}" || contains "${unit}" ${started_units[@]+"${started_units[@]}"}; }; then
            admin systemctl stop "${unit}" || stuck_units+=("${unit}")
          fi
          ;;
      esac
    done
    # A fresh install has no previous version for a unit it could not stop or
    # disable; leave that unit its runtime rather than a missing one.
    if [ "${venv_before}" = absent ] && [ "${#stuck_units[@]}" -gt 0 ]; then
      venv_restored=false
    elif ! restore_venv; then
      venv_restored=false
      quoted_new_runtime=$(printf '%q' "${new_runtime}")
      echo "Could not switch back to the previous version; the new one stays active, kept in ${quoted_new_runtime}." >&2
      if [ "${venv_before}" = directory ] && [ -d "${legacy_runtime}/.venv" ]; then
        quoted_legacy_runtime=$(printf '%q' "${legacy_runtime}")
        echo "The previous version is kept in ${quoted_legacy_runtime}." >&2
      fi
      restart_on_new_runtime
    fi
  fi
  if [ "${units_recorded}" != true ]; then
    return 0
  fi
  for unit in "${managed_units[@]}"; do
    if [ ! -e "${unit_root}/${unit}" ]; then
      continue
    fi
    # A timer whose service could not be put back has nothing to start; it is
    # not turned back on, and is named below.
    startable=true
    if [[ "${unit}" == *.timer ]] && [ ! -e "${unit_root}/${unit%.timer}.service" ]; then
      startable=false
    fi
    if contains "${unit}" ${enabled_before[@]+"${enabled_before[@]}"}; then
      [ "${startable}" = false ] || systemctl is-enabled --quiet "${unit}" || \
        admin systemctl enable "${unit}" || true
    elif systemctl is-enabled --quiet "${unit}"; then
      admin systemctl disable "${unit}" || true
    fi
    if long_running "${unit}"; then
      if contains "${unit}" ${active_before[@]+"${active_before[@]}"}; then
        [ "${startable}" = false ] || running "${unit}" || \
          admin systemctl start "${unit}" || true
      elif running "${unit}"; then
        admin systemctl stop "${unit}" || true
      fi
    fi
  done
}

# Name every unit that is not back as it was, from what systemd reports now.
report_restore() {
  local unit
  local stopped=()
  local running=()
  local changed=()
  if [ "${units_recorded}" != true ]; then
    return 0
  fi
  for unit in "${managed_units[@]}"; do
    if [ -e "${unit_root}/${unit}" ]; then
      if [ "${units_replaced}" = true ] && { [ ! -f "${unit_backup_dir}/${unit}" ] || \
          ! cmp -s "${unit_backup_dir}/${unit}" "${unit_root}/${unit}"; }; then
        changed+=("${unit}")
      elif contains "${unit}" ${enabled_before[@]+"${enabled_before[@]}"}; then
        systemctl is-enabled --quiet "${unit}" || changed+=("${unit}")
      elif systemctl is-enabled --quiet "${unit}"; then
        changed+=("${unit}")
      fi
    elif [ -f "${unit_backup_dir}/${unit}" ]; then
      changed+=("${unit}")
    fi
    # A oneshot cycle running now was started by a timer put back on the
    # previous version; one that kept running the new version is in stuck_units.
    if contains "${unit}" ${active_before[@]+"${active_before[@]}"}; then
      systemctl is-active --quiet "${unit}" || stopped+=("${unit}")
    elif { long_running "${unit}" || [ "${runtime_activated}" = true ]; } && \
        systemctl is-active --quiet "${unit}"; then
      running+=("${unit}")
    fi
  done
  if [ "${#running[@]}" -gt 0 ]; then
    echo "The upgrade failed and these services could not be stopped: ${running[*]}." >&2
    echo "Stop them with: sudo systemctl stop ${running[*]}" >&2
  fi
  if [ "${#stuck_units[@]}" -gt 0 ]; then
    quoted_new_runtime=$(printf '%q' "${new_runtime}")
    echo "These services could not be stopped or disabled and may still run the new version, kept in ${quoted_new_runtime}: ${stuck_units[*]}." >&2
  fi
  if [ "${#stopped[@]}" -gt 0 ]; then
    echo "The upgrade failed and these services are stopped: ${stopped[*]}." >&2
    echo "Start them with: sudo systemctl start ${stopped[*]}" >&2
  fi
  if [ "${#changed[@]}" -gt 0 ]; then
    echo "These services are not set up as they were before the upgrade: ${changed[*]}." >&2
    echo "Check them with: systemctl status ${changed[*]}" >&2
  fi
  if [ "${#running[@]}" -eq 0 ] && [ "${#stopped[@]}" -eq 0 ] && [ "${#changed[@]}" -eq 0 ] && \
      [ "${#stuck_units[@]}" -eq 0 ] && [ "${venv_restored}" = true ]; then
    if [ "${#stopped_units[@]}" -gt 0 ]; then
      echo "The upgrade failed; the services it stopped were started again on the previous version." >&2
    elif [ "${runtime_activated}" = true ] && [ "${venv_before}" != absent ]; then
      echo "The upgrade failed; the previous version is running again." >&2
    fi
  fi
}

cleanup() {
  status=$?
  trap - EXIT
  # One failed step must not cut the rest of the cleanup short.
  set +e
  # Finish cleaning up even if a stop signal arrives, then report it.
  cleanup_stop=""
  trap 'cleanup_stop=129' HUP
  trap 'cleanup_stop=130' INT
  trap 'cleanup_stop=143' TERM
  # Nothing this run started may still run while it puts things back.
  stop_bounded
  release_writer_locks
  if [ "${installation_complete}" != true ]; then
    restore_units
    report_restore
    if [ -n "${new_runtime}" ] && [ "${#stuck_units[@]}" -eq 0 ] && \
        [ "${venv_restored}" = true ] && [ "${new_runtime_linked}" != true ] && \
        ! { contained "${runtimes_dir}" "${new_runtime}" && rm -rf "${new_runtime}"; }; then
      leftovers+=("${new_runtime}")
    fi
    if [ "${#leftovers[@]}" -gt 0 ]; then
      quoted_leftovers=$(printf '%q ' "${leftovers[@]}")
      echo "Could not remove what this upgrade added; nothing uses it, so remove it by hand: ${quoted_leftovers% }" >&2
    fi
    if [ "${created_runtimes_dir}" = true ]; then
      { contained "${app_dir}" "${runtimes_dir}" && rmdir "${runtimes_dir}" 2>/dev/null; } || true
    fi
    if [ "${status}" -eq 0 ]; then
      status=1
    fi
  elif [ "${status}" -ne 0 ]; then
    echo "The new version is installed and running, but a final step failed; see the error above." >&2
    if [ -n "${previous_runtime}" ]; then
      quoted_previous_runtime=$(printf '%q' "${previous_runtime}")
      echo "Previous version kept for rollback: ${quoted_previous_runtime}" >&2
    fi
  fi
  if [ -n "${work_dir}" ] && ! rm -rf "${work_dir}"; then
    quoted_work_dir=$(printf '%q' "${work_dir}")
    echo "Could not remove temporary files; remove them by hand: ${quoted_work_dir}" >&2
  fi
  if [ -n "${cleanup_stop}" ] && [ "${status}" -eq 0 ]; then
    echo "Stopped while finishing; the new version is installed and running." >&2
    status=${cleanup_stop}
  fi
  exit "${status}"
}
trap cleanup EXIT
# A stopped installer still restores what it changed, then reports the signal.
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
# Work folders live where the rerun after a power loss finds them: beside the
# configuration, or in a system service's install root, which only root can change.
if [ -z "${service_account}" ]; then
  mkdir -p "${support_dir}"
  make_temp_dir work_dir "${support_dir}/install.XXXXXX"
else
  make_temp_dir work_dir "${install_root}/install.XXXXXX"
fi
unit_dir="${work_dir}/units"
unit_backup_dir="${work_dir}/previous-units"
mkdir "${unit_dir}" "${unit_backup_dir}"

ready_dir=${work_dir}
if [ ! -d "${runtimes_dir}" ]; then
  created_runtimes_dir=true
fi
if [ -z "${service_account}" ]; then
  chmod 600 "${config_path}"
  mkdir -p "${app_dir}/catalog" "${app_dir}/deploy" "${runtimes_dir}" "${support_dir}"
else
  # The writer lock holder, which runs as the account, says here that it holds them.
  ready_dir="${work_dir}/account"
  mkdir "${ready_dir}"
  chown "${service_account}:${account_group}" "${ready_dir}"
  chmod 711 "${work_dir}"
  if ! hidden=$(account_can_read "${ready_dir}"); then
    unsafe_path=${ready_dir}
    eval "${hidden}"
    quoted_unsafe_path=$(printf '%q' "${unsafe_path}")
    echo "${service_account} cannot reach ${quoted_unsafe_path}. No changes made." >&2
    exit 1
  fi
  mkdir -p "${app_dir}/catalog" "${app_dir}/deploy" "${runtimes_dir}"
  # The account's folders it does not own yet: missing, or empty ones a run
  # stopped before it gave them away. Their folder is root's, so none of them
  # can be a link here.
  for name in ${give_directories}; do
    if [ ! -d "${install_root}/${name}" ]; then
      mkdir -m 0700 "${install_root}/${name}"
    fi
    chown "${service_account}:${account_group}" "${install_root}/${name}"
  done
  as_account chmod 600 "${config_path}"
  # Created empty if missing; an existing file is never rewritten or shown.
  # shellcheck disable=SC2016 # $1 expands in the account's shell, not here.
  as_account sh -c 'test -e "$1" || (umask 077 && : >>"$1")' sh "${environment_file}"
fi
runtimes_dir=$(cd -P "${runtimes_dir}" && pwd)

# Build and check the new runtime beside the live one; nothing running changes.
make_temp_dir new_runtime "${runtimes_dir}/runtime.XXXXXX"
contained "${runtimes_dir}" "${new_runtime}"
share_with_account "${new_runtime}"
stage_runtime_source "${new_runtime}"
# A system service keeps the interpreter uv may download with its code, where
# the account can run but not change it, and compiles bytecode it cannot write.
uv_environment=()
uv_options=()
if [ -n "${service_account}" ]; then
  uv_environment=("UV_PYTHON_INSTALL_DIR=${app_dir}/.python")
  uv_options=(--compile-bytecode)
fi
# A stray UV_PROJECT_ENVIRONMENT would build the venv somewhere the units do not run.
env -u UV_PROJECT_ENVIRONMENT ${uv_environment[@]+"${uv_environment[@]}"} "${uv_bin}" sync \
  --project "${new_runtime}" --python "${python_version}" --extra controller --locked \
  ${uv_options[@]+"${uv_options[@]}"}
# uv leaves some files writable by everyone (its .venv/.lock): the account
# must not be able to change any of the code it runs. That includes a runtime
# an interrupted run (a power loss) left before this step, which this run
# removes once it succeeds. The tree is under the install root, and chmod -R
# follows no links.
if [ -n "${service_account}" ]; then
  contained "${app_dir}" "${runtimes_dir}"
  chmod -R go-w "${runtimes_dir}"
fi
# The account runs this build: it must be able to read all of it, whatever
# modes uv's cache gave the files.
if ! hidden=$(account_can_read "${new_runtime}"); then
  unsafe_path=${new_runtime}
  eval "${hidden}"
  quoted_unsafe_path=$(printf '%q' "${unsafe_path}")
  echo "${service_account} cannot read ${quoted_unsafe_path} in the new version. No changes made." >&2
  exit 1
fi
# The interpreter uv downloads is code too, and uv leaves a lock file beside
# it that anyone can write. The folder is root's, under the install root.
python_dir="${app_dir}/.python"
if [ -n "${service_account}" ] && [ -e "${python_dir}" ]; then
  contained "${app_dir}" "${python_dir}"
  chmod -R go-w "${python_dir}"
  if ! hidden=$(account_can_read "${python_dir}"); then
    unsafe_path=${python_dir}
    eval "${hidden}"
    quoted_unsafe_path=$(printf '%q' "${unsafe_path}")
    echo "${service_account} cannot read ${quoted_unsafe_path} in the new version. No changes made." >&2
    exit 1
  fi
fi
# The CLI reports a rejection as a JSON envelope on stdout; show its message.
if ! validation=$(as_account "${new_runtime}/.venv/bin/inky-bird" config validate \
    --config "${config_path}"); then
  echo "The new version cannot use this configuration. No changes made." >&2
  python3 -I -c '
import json, sys
try:
    print(json.loads(sys.argv[1])["error"]["message"])
except (ValueError, KeyError, TypeError):
    print("The configuration check failed without a readable reason.")
' "${validation}" >&2
  exit 1
fi
if [ -n "${stopped_hook}" ] && ! stopped_hook_state_dir=$(as_account \
    "${new_runtime}/.venv/bin/python" -I \
    -m inky_bird_frame.upgrade_gates state-dir "${config_path}"); then
  echo "Could not read the state directory for the stopped-boundary hook. No changes made." >&2
  exit 1
fi

hold_writer_locks false
remove_stale_work
# Waiting for a plate can outlast cached sudo credentials; ask again before stopping anything.
if ! admin_ready; then
  echo "Administrator access is required to install system services. No changes made." >&2
  exit 1
fi
record_units
# App-owned installations use the same dynamic scheduler as Docker. Stop it
# before replacing its runtime; a managed installation removes it below.
if systemctl is-active --quiet "${service_prefix}-scheduler.service"; then
  request_stop "${service_prefix}-scheduler.service"
fi
for name in refresh generate catalog-publish notifications; do
  timer="${service_prefix}-${name}.timer"
  if systemctl is-active --quiet "${timer}"; then
    stopped_units+=("${timer}")
    admin systemctl stop "${timer}"
  fi
done
# The scheduler, and a job a timer started, may be waiting for one of the
# writer locks to finish; let go of them rather than wait for each other.
release_writer_locks
deadline=$((SECONDS + quiesce_timeout_seconds))
wait_until_stopped scheduler refresh generate catalog-publish notifications
if [ -n "${stopped_hook}" ]; then
  if ! admin_ready; then
    echo "Administrator access is required to install system services." >&2
    exit 1
  fi
  # The hook runs with every service stopped; the web service starts again
  # with the others below.
  if systemctl is-active --quiet "${service_prefix}-controller.service"; then
    hold_writer_locks true
    request_stop "${service_prefix}-controller.service"
    release_writer_locks
    deadline=$((SECONDS + quiesce_timeout_seconds))
    wait_until_stopped controller
  fi
  if [ -n "${service_account}" ] && ! account_processes_gone; then
    echo "Processes of ${service_account} still run after its services stopped; the upgrade did not switch to the new version." >&2
    exit 1
  fi
  hold_writer_locks true
  run_stopped_hook
  release_writer_locks
fi
# The wait above can also outlast cached credentials; the steps below need them.
if ! admin_ready; then
  echo "Administrator access is required to install system services." >&2
  exit 1
fi

# Check the configuration, sync the catalog and render the units, with the
# interpreter and arguments given. A system service runs it twice: as its
# account to check the configuration and sync the catalog ("sync"), then as
# root, which reads no secret, to render its units ("render").
controller_program() {
  "$@" <<'PY'
import os
import sys
from pathlib import Path

from inky_bird_frame.catalog import catalog_state_lock
from inky_bird_frame.config import DiscoveryProvider, load_config
from inky_bird_frame.errors import ConfigurationError
from inky_bird_frame.installation import controller_systemd_units, native_oidc_environment
from inky_bird_frame.publisher import sync_public_catalog


unit_dir, root, app_dir, config_path, home = map(Path, sys.argv[1:6])
user = sys.argv[6]
mode = sys.argv[7] if len(sys.argv) > 7 else ""
system = None
if mode == "render":
    from inky_bird_frame.installation import SystemService, read_account_config

    import pwd
    import shlex

    from inky_bird_frame.installation import SYSTEM_SERVICE_MARKER, system_service_problem

    config = read_account_config(config_path, int(sys.argv[10]))
    controller = config.controller
    writable = (config_path.parent, home, controller.workspace_dir, controller.catalog_dir)
    system = SystemService(sys.argv[8], Path(sys.argv[9]), (*writable, controller.state_dir))
    # Other system services' web services on this machine, by their own units.
    others = []
    for unit in sorted(Path(sys.argv[11]).glob("inky-bird*-controller.service")):
        try:
            lines = unit.read_text().splitlines()
            other = next(line.partition("=")[2] for line in lines if line.startswith("User="))
            if lines[:1] != [SYSTEM_SERVICE_MARKER] or other == user:
                continue
            if unit.name != f"{other}-controller.service":
                continue
            start = shlex.split(
                next(line.partition("=")[2] for line in lines if line.startswith("ExecStart="))
            )
            other_config = Path(start[start.index("--config") + 1])
            owner = pwd.getpwnam(other).pw_uid
            others.append((other, read_account_config(other_config, owner)))
        except (StopIteration, KeyError, ValueError, OSError, ConfigurationError):
            continue
    problem = system_service_problem(config, config_path.parent.parent, others)
    if problem is not None:
        raise ConfigurationError(problem)
elif mode == "sync":
    from inky_bird_frame.installation import (
        load_private_environment,
        validate_native_controller_setup,
    )

    load_private_environment()
    config = load_config(config_path)
    validate_native_controller_setup(config, config_path, system_service=True)
else:
    config = load_config(config_path)
environment_credentials = []
if config.discovery.geoapify_api_key_env:
    environment_credentials.append("geoapify_api_key_env")
if DiscoveryProvider.EBIRD in config.discovery.sources and config.discovery.ebird_api_key_env:
    environment_credentials.append("ebird_api_key_env")
if (
    DiscoveryProvider.BIRDWEATHER in config.discovery.sources
    and config.discovery.birdweather_token_env
):
    environment_credentials.append("birdweather_token_env")
if environment_credentials and not mode:
    names = ", ".join(environment_credentials)
    raise ConfigurationError(
        f"The systemd installer cannot use {names}; store the corresponding credential "
        "directly in the private config file"
    )
environment_destinations = [
    destination.name
    for destination in config.notifications.destinations
    if config.notifications.enabled and destination.url_env is not None
]
if environment_destinations and not mode:
    names = ", ".join(environment_destinations)
    raise ConfigurationError(
        "The systemd installer requires direct notification URLs in the private "
        f"config file; replace url_env for: {names}"
    )
if not mode:
    native_oidc_environment(config, config_path)
if mode != "render":
    config.controller.workspace_dir.mkdir(parents=True, exist_ok=True)
    config.controller.catalog_dir.parent.mkdir(parents=True, exist_ok=True)
    with catalog_state_lock(config.controller.state_dir):
        source_catalog = root / "catalog"
        managed_catalog = app_dir / "catalog"
        if root != app_dir and managed_catalog.resolve() != config.controller.catalog_dir.resolve():
            sync_public_catalog(
                source_catalog,
                managed_catalog,
                allow_replacements=True,
                retain_independent_approvals=True,
            )
            source_catalog = managed_catalog
        sync_public_catalog(
            source_catalog,
            config.controller.catalog_dir,
            allow_replacements=True,
            retain_independent_approvals=True,
        )
if mode == "sync":
    sys.exit(0)

service_prefix = os.environ["INKY_BIRD_SERVICE_PREFIX"]
# The command named like the services: 0.9 installs had only inky-bird-frame,
# so a service whose definition a failed upgrade could not put back still starts.
# A system service is named after its account; it runs the canonical command.
command_name = "inky-bird" if mode == "render" else service_prefix
executable = app_dir / ".venv/bin" / command_name
units = controller_systemd_units(
    config,
    executable=executable,
    app_dir=app_dir,
    config_path=config_path,
    home=home,
    user=user,
    service_prefix=service_prefix,
    system=system,
)
for name, content in units.items():
    (unit_dir / name).write_text(content)
PY
}

if [ -z "${service_account}" ]; then
  controller_program "${new_runtime}/.venv/bin/python" - \
    "${unit_dir}" "${catalog_root}" "${app_dir}" "${config_path}" "${HOME}" "${user}"
else
  # Root copies the public catalog into the code it keeps; the account syncs
  # its own catalog from there.
  if [ "${catalog_root}" != "${app_dir}" ]; then
    contained "${app_dir}" "${app_dir}/catalog"
    rsync -a --delete "${copy_options[@]}" "${catalog_root}/catalog/" "${app_dir}/catalog/"
    share_copy "${app_dir}/catalog"
  fi
  controller_program as_account "${new_runtime}/.venv/bin/python" - \
    "${unit_dir}" "${app_dir}" "${app_dir}" "${config_path}" "${account_home}" "${user}" sync
  controller_program "${new_runtime}/.venv/bin/python" - \
    "${unit_dir}" "${app_dir}" "${app_dir}" "${config_path}" "${account_home}" "${user}" \
    render "${account_group}" "${environment_file}" "${account_uid}" "${unit_root}"
fi

# Only an unreachable bird source may wait for the scheduled refresh; any other
# failure means the new version does not work here.
refresh_status=0
refresh_output=$(as_account "${new_runtime}/.venv/bin/inky-bird" refresh --config "${config_path}") || \
  refresh_status=$?
printf '%s\n' "${refresh_output}"
if [ "${refresh_status}" -ne 0 ]; then
  if ! python3 -I -c '
import json, sys
try:
    envelope = json.loads(sys.argv[1])
    sys.exit(0 if envelope["ok"] is False and envelope["error"]["type"] == "data_source" else 1)
except (ValueError, KeyError, TypeError):
    sys.exit(1)
' "${refresh_output}"; then
    echo "The new version could not refresh bird observations; see the error above." >&2
    exit 1
  fi
  echo "Could not fetch bird observations yet. The scheduled refresh will try again." >&2
fi

if [ -f "${unit_dir}/${service_prefix}-catalog-publish.timer" ]; then
  as_account "${new_runtime}/.venv/bin/inky-bird" catalog-publish \
    --config "${config_path}" --dry-run
fi

# The account must not be able to change the code it is about to run.
if [ -n "${service_account}" ] && ! code_check=$(python3 -I "${system_helper}" verify-code \
    "${service_account}" "${install_root}"); then
  unsafe_path=${app_dir}
  eval "${code_check}"
  quoted_unsafe_path=$(printf '%q' "${unsafe_path}")
  echo "${service_account} could change ${quoted_unsafe_path}; the upgrade did not switch to the new version." >&2
  exit 1
fi
activate_runtime
units_replaced=true
for unit in "${unit_dir}"/*; do
  admin install -m 0644 "${unit}" "${unit_root}/$(basename "${unit}")"
done
for optional in refresh generate catalog-publish notifications; do
  if [ ! -f "${unit_dir}/${service_prefix}-${optional}.timer" ]; then
    if [ -e "${unit_root}/${service_prefix}-${optional}.timer" ]; then
      admin systemctl disable --now "${service_prefix}-${optional}.timer"
    fi
    admin rm -f \
      "${unit_root}/${service_prefix}-${optional}.service" \
      "${unit_root}/${service_prefix}-${optional}.timer"
  fi
done
if [ ! -f "${unit_dir}/${service_prefix}-scheduler.service" ]; then
  if [ -e "${unit_root}/${service_prefix}-scheduler.service" ]; then
    admin systemctl disable --now "${service_prefix}-scheduler.service"
  fi
  admin rm -f "${unit_root}/${service_prefix}-scheduler.service"
fi

admin systemctl daemon-reload
admin systemctl enable "${service_prefix}-controller.service"
if [ -f "${unit_dir}/${service_prefix}-refresh.timer" ]; then
  admin systemctl enable "${service_prefix}-refresh.timer"
fi
if [ -f "${unit_dir}/${service_prefix}-scheduler.service" ]; then
  admin systemctl enable "${service_prefix}-scheduler.service"
fi
if [ -f "${unit_dir}/${service_prefix}-generate.timer" ]; then
  admin systemctl enable "${service_prefix}-generate.timer"
fi
if [ -f "${unit_dir}/${service_prefix}-catalog-publish.timer" ]; then
  admin systemctl enable "${service_prefix}-catalog-publish.timer"
fi
if [ -f "${unit_dir}/${service_prefix}-notifications.timer" ]; then
  admin systemctl enable "${service_prefix}-notifications.timer"
fi
started_units+=("${service_prefix}-controller.service")
admin systemctl restart "${service_prefix}-controller.service"
if [ -f "${unit_dir}/${service_prefix}-refresh.timer" ]; then
  admin systemctl restart "${service_prefix}-refresh.timer"
fi
if [ -f "${unit_dir}/${service_prefix}-scheduler.service" ]; then
  started_units+=("${service_prefix}-scheduler.service")
  admin systemctl restart "${service_prefix}-scheduler.service"
fi
if [ -f "${unit_dir}/${service_prefix}-generate.timer" ]; then
  admin systemctl restart "${service_prefix}-generate.timer"
fi
if [ -f "${unit_dir}/${service_prefix}-catalog-publish.timer" ]; then
  admin systemctl restart "${service_prefix}-catalog-publish.timer"
fi
if [ -f "${unit_dir}/${service_prefix}-notifications.timer" ]; then
  admin systemctl restart "${service_prefix}-notifications.timer"
fi

systemctl is-enabled --quiet "${service_prefix}-controller.service"
systemctl is-active --quiet "${service_prefix}-controller.service"
if [ -f "${unit_dir}/${service_prefix}-refresh.timer" ]; then
  systemctl is-enabled --quiet "${service_prefix}-refresh.timer"
  systemctl is-active --quiet "${service_prefix}-refresh.timer"
fi
if [ -f "${unit_dir}/${service_prefix}-scheduler.service" ]; then
  systemctl is-enabled --quiet "${service_prefix}-scheduler.service"
  systemctl is-active --quiet "${service_prefix}-scheduler.service"
fi
if [ -f "${unit_dir}/${service_prefix}-generate.timer" ]; then
  systemctl is-enabled --quiet "${service_prefix}-generate.timer"
  systemctl is-active --quiet "${service_prefix}-generate.timer"
fi
if [ -f "${unit_dir}/${service_prefix}-catalog-publish.timer" ]; then
  systemctl is-enabled --quiet "${service_prefix}-catalog-publish.timer"
  systemctl is-active --quiet "${service_prefix}-catalog-publish.timer"
fi
if [ -f "${unit_dir}/${service_prefix}-notifications.timer" ]; then
  systemctl is-enabled --quiet "${service_prefix}-notifications.timer"
  systemctl is-active --quiet "${service_prefix}-notifications.timer"
fi
if [ -f "${unit_dir}/${service_prefix}-controller.service" ]; then
  check_new_serve
fi

installation_complete=true
# The managed directory's copy of this script reinstalls the active runtime;
# the mark tells it apart from a source checkout used as that directory.
if [ ! "${root}/deploy" -ef "${app_dir}/deploy" ]; then
  install -m 0755 "${root}/deploy/install-controller-systemd.sh" "${app_dir}/deploy/"
  : >"${app_dir}/deploy/.managed-copy"
fi
# Keep the previous runtime for rollback; nothing runs from anything older.
for runtime in "${runtimes_dir}"/*; do
  if [ ! -d "${runtime}" ] || [ "${runtime}" = "${new_runtime}" ] || \
      [ "${runtime}" = "${previous_runtime}" ]; then
    continue
  fi
  # This script and the files it reads are in the runtime it was run from.
  if [ "${runtime}" -ef "${root}" ]; then
    quoted_runtime=$(printf '%q' "${runtime}")
    echo "The runtime this installer ran from is kept in ${quoted_runtime}." >&2
  elif ! { contained "${runtimes_dir}" "${runtime}" && rm -rf "${runtime}"; }; then
    quoted_runtime=$(printf '%q' "${runtime}")
    echo "Could not remove an older runtime; remove it by hand: ${quoted_runtime}" >&2
  fi
done
quoted_root=$(printf '%q' "${root}")
quoted_app_dir=$(printf '%q' "${app_dir}")
echo "Controller installed from ${quoted_root} into ${quoted_app_dir}."
quoted_config_path=$(printf '%q' "${config_path}")
echo "Configuration: ${quoted_config_path}"
if [ -n "${previous_runtime}" ]; then
  quoted_previous_runtime=$(printf '%q' "${previous_runtime}")
  echo "Previous version kept for rollback: ${quoted_previous_runtime}"
fi
if ! as_account "${app_dir}/.venv/bin/inky-bird" access setup --if-needed --config "${config_path}"; then
  echo "Controller installation completed; owner setup is incomplete. Run access setup in a private terminal. Display and scheduled work continue." >&2
fi
