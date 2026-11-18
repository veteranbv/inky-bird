#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
  echo "Controller installer currently supports macOS." >&2
  exit 1
fi
# Printed commands quote what they name, so one can be pasted as it is.
script_command=$(printf '%q' "$0")
daemons_dir="/Library/LaunchDaemons"
system_requested=false
for argument in "$@"; do
  if [ "${argument}" = --service-account ]; then
    system_requested=true
  fi
done
# The account a LaunchDaemon this installer made runs as: it is named after it.
daemon_account() {
  python3 -I -c '
import plistlib, sys
try:
    with open(sys.argv[1], "rb") as handle:
        job = plistlib.load(handle)
    print(job.get("UserName", "") if isinstance(job, dict) else "")
except Exception:
    print("")
' "$1"
}
current_uid=$(id -u)
if [ "${current_uid}" = 0 ] && [ "${system_requested}" = false ]; then
  for plist in "${daemons_dir}"/com.inky-bird*.serve.plist; do
    owner=""
    if [ -f "${plist}" ]; then
      owner=$(daemon_account "${plist}")
    fi
    if [ -n "${owner}" ] && [ "${plist##*/}" = "com.${owner}.serve.plist" ]; then
      echo "${plist##*/} is a system service of ${owner}. Run the installer again with --service-account ${owner}. No changes made." >&2
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
# Check both families before even starting Python (macOS may initialize its
# interpreter cache under HOME). A user installer must not touch system jobs.
for family in inky-bird inky-bird-frame; do
  for label in serve refresh generate catalog-publish notifications scheduler controller-cycle; do
    if [ "${system_requested}" = true ]; then
      break 2
    fi
    system_plist="${daemons_dir}/com.${family}.${label}.plist"
    if [ -e "${system_plist}" ] || [ -L "${system_plist}" ] || \
        launchctl print "system/com.${family}.${label}" >/dev/null 2>&1; then
      serve_daemon="${daemons_dir}/com.${family}.serve.plist"
      if [ -f "${serve_daemon}" ] && [ "$(daemon_account "${serve_daemon}")" = "${family}" ]; then
        echo "com.${family}.serve is a system service of ${family}. Upgrade it as root: sudo ${script_command} --service-account ${family}. No changes made." >&2
        exit 1
      fi
      echo "Inky uses operator-managed system services. Use that deployment process to upgrade; the user installer made no changes." >&2
      exit 1
    fi
  done
done
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

service_prefix="" app_dir="" support_dir="" config_path=""
account_uid="" account_group="" account_home="" problem="" unsafe_path=""
give_directories=""
if [ -z "${service_account}" ]; then
  native_names=$(python3 "${root}/src/inky_bird_frame/native_names.py")
  eval "${native_names}"
else
  # A system service: the account names the jobs and owns the data; the
  # code under ${install_root}/app is root's alone.
  umask 022
  install_root=${install_root:-/opt/${service_account}}
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
  support_dir="${install_root}/config"
  config_path="${support_dir}/config.toml"
  environment_file="${support_dir}/private.env"
fi
export INKY_BIRD_SERVICE_PREFIX="${service_prefix}"
uv_bin=${UV_BIN:-}
if [ -z "${uv_bin}" ]; then
  uv_bin=$(command -v uv || true)
fi
python_version=${INKY_BIRD_PYTHON_VERSION:-3.11}
quiesce_timeout_seconds=1800
poll_seconds=1
health_timeout_seconds=90
log_dir="${support_dir}/logs"
agents_dir="${HOME}/Library/LaunchAgents"
# The launchd domain the jobs run in: this user's session, or the system's.
domain="gui/${current_uid}"
if [ -n "${service_account}" ]; then
  log_dir="${install_root}/logs"
  agents_dir=${daemons_dir}
  domain=system
fi
# Where launchd reads the jobs, which a first install writes elsewhere first.
export INKY_BIRD_JOBS_DIR="${agents_dir}"
serve_plist="${agents_dir}/com.${service_prefix}.serve.plist"
refresh_plist="${agents_dir}/com.${service_prefix}.refresh.plist"
generation_plist="${agents_dir}/com.${service_prefix}.generate.plist"
catalog_publish_plist="${agents_dir}/com.${service_prefix}.catalog-publish.plist"
notifications_plist="${agents_dir}/com.${service_prefix}.notifications.plist"
scheduler_plist="${agents_dir}/com.${service_prefix}.scheduler.plist"
legacy_cycle_plist="${agents_dir}/com.${service_prefix}.controller-cycle.plist"
agent_plists=(serve_plist refresh_plist generation_plist catalog_publish_plist \
  notifications_plist scheduler_plist)
# Point every agent definition variable at the same file names in folder $1.
place_agents() {
  local name file
  for name in "${agent_plists[@]}"; do
    file=${!name}
    printf -v "${name}" '%s/%s' "$1" "${file##*/}"
  done
}
# Each runtime holds one source copy and the venv built from it;
# ${app_dir}/.venv links to the active one.
runtimes_dir="${app_dir}/.runtimes"
# setup --yes from the installed CLI runs the active runtime's own copy of this
# script. A runtime holds no catalog; that stays in the managed directory.
catalog_root=${root}
if [ "$(cd -P "${root}/.." && pwd)" = "$(cd -P "${runtimes_dir}" 2>/dev/null && pwd || true)" ]; then
  catalog_root=${app_dir}
fi

restore_catalog_publisher_schedule() {
  local uid=$1
  local recovery_dir
  local recovery_plist
  local status=0
  # A system service's support folder is the account's; root keeps its own copy.
  recovery_dir=$(mktemp -d "${recovery_parent}/catalog-publish-recovery.XXXXXX") || return 1
  recovery_plist="${recovery_dir}/catalog-publish.plist"
  cp "${catalog_publish_plist}" "${recovery_plist}" && \
    /usr/bin/plutil -replace RunAtLoad -bool false "${recovery_plist}" && \
    launchctl bootstrap "${domain}" "${recovery_plist}" || status=1
  rm -rf "${recovery_dir}" || leftovers+=("${recovery_dir}")
  return "${status}"
}

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

# Copy what building, and later reinstalling, a runtime needs from ${root}.
stage_runtime_source() {
  local target=$1
  rsync -a --delete ${copy_options[@]+"${copy_options[@]}"} "${root}/src/" "${target}/src/"
  mkdir -p "${target}/deploy" "${target}/docs"
  install -m 0755 "${root}/deploy/install-controller.sh" "${target}/deploy/"
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

# A system service's installer runs as root: it changes the code and the jobs
# itself, and runs the application as the account, with only the environment
# given here (see system_service.py run-as for why not sudo -u).
account_prefix=()
copy_options=()
presence_timeout_seconds=30
recovery_parent=${support_dir}
if [ -n "${service_account}" ]; then
  # A copy is root's, readable by everyone, whatever owner and modes the
  # checkout's files have (a root umask of 077 makes them 0600).
  copy_options=(--no-owner --no-group --chmod=D0755 --chmod=F0644)
  account_prefix=(python3 -I "${system_helper}" run-as "${service_account}" --
    env -i "PATH=/usr/bin:/bin:/usr/sbin:/sbin" "HOME=${account_home}" "LANG=en_US.UTF-8"
    "INKY_BIRD_ENV_FILE=${environment_file}")
fi
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
if [ -n "${service_account}" ]; then
  # The user who runs sudo may have this installation's jobs as LaunchAgents.
  for label in serve refresh generate catalog-publish notifications scheduler; do
    if [ -n "${SUDO_UID:-}" ] && \
        launchctl print "gui/${SUDO_UID}/com.${service_prefix}.${label}" >/dev/null 2>&1; then
      agent_owner=${SUDO_USER:-${SUDO_UID}}
      echo "com.${service_prefix}.${label} is a LaunchAgent of ${agent_owner}; this installer does not move it. Remove it with deploy/uninstall-controller.sh run as that user, then run this again. No changes made." >&2
      exit 1
    fi
  done
else
  chmod 600 "${config_path}"
fi
if [ -z "${uv_bin}" ] || [ ! -x "${uv_bin}" ]; then
  echo "uv is not executable. Install uv or set UV_BIN." >&2
  exit 1
fi

created_runtimes_dir=false
if [ ! -d "${runtimes_dir}" ]; then
  created_runtimes_dir=true
fi
if [ -z "${service_account}" ]; then
  mkdir -p "${app_dir}/catalog" "${app_dir}/deploy" "${runtimes_dir}" "${support_dir}" \
    "${log_dir}" "${agents_dir}"
  chmod 700 "${support_dir}" "${log_dir}"
  find "${log_dir}" -maxdepth 1 -type f -name '*.log*' -exec chmod 600 {} +
  runtimes_dir=$(cd -P "${runtimes_dir}" && pwd)
fi
uid=${current_uid}
work_dir=""
plist_backup_dir=""
new_runtime=""
previous_runtime=""
legacy_runtime=""
venv_before=""
runtime_activated=false
installation_complete=false
writer_lock_pid=""
writer_lock_holds=0
writer_locks_held=false
writer_locks_unavailable=false
bounded_pid=""
bounded_timed_out=false
stopped_hook_ran=false
stopped_hook_state_dir=""
agents_recorded=false
previously_loaded_labels=()
stopped_labels=()
stopping_label=""
unrestored=()
still_running=()
leftovers=()
new_runtime_linked=false

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
  writer_locks_held=true
}

release_writer_locks() {
  if [ -n "${writer_lock_pid}" ]; then
    stop_group "${writer_lock_pid}"
    writer_lock_pid=""
  fi
  writer_locks_held=false
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

# Point ${app_dir}/.venv, the path every agent runs, at the new runtime. The
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
    # cannot leave the jobs with no venv at all.
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


# An agent this run stopped or loaded must not run while the runtime switches
# back; an agent it never touched keeps running.
stop_agent_for_restore() {
  local label=$1
  local agent="com.${service_prefix}.${label}"
  if { contains "${label}" ${stopped_labels[@]+"${stopped_labels[@]}"} || \
      ! contains "${label}" ${previously_loaded_labels[@]+"${previously_loaded_labels[@]}"}; } && \
      launchctl print "${domain}/${agent}" >/dev/null 2>&1; then
    if ! launchctl bootout "${domain}/${agent}"; then
      still_running+=("${agent}")
    fi
  fi
}

# Put one agent back as it was: its saved plist, and running again only if
# this run stopped it.
restore_agent() {
  local label=$1
  local agent="com.${service_prefix}.${label}"
  local plist="${agents_dir}/com.${service_prefix}.${label}.plist"
  local backup="${plist_backup_dir}/com.${service_prefix}.${label}.plist"
  if contains "${agent}" ${still_running[@]+"${still_running[@]}"}; then
    return 0
  fi
  if [ -f "${backup}" ]; then
    if ! cp "${backup}" "${plist}"; then
      unrestored+=("${agent}")
      return 0
    fi
  elif [ -e "${plist}" ] && ! rm -f "${plist}"; then
    unrestored+=("${agent}")
    return 0
  fi
  if ! contains "${label}" ${stopped_labels[@]+"${stopped_labels[@]}"}; then
    return 0
  fi
  if [ ! -f "${backup}" ]; then
    unrestored+=("${agent}")
  elif [ "${label}" = "catalog-publish" ]; then
    restore_catalog_publisher_schedule "${uid}" || unrestored+=("${agent}")
  else
    launchctl bootstrap "${domain}" "${plist}" || unrestored+=("${agent}")
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
    runtime_restored=true
    # A stop signal can arrive once an agent is out but before it is recorded.
    if [ -n "${stopping_label}" ] && \
        ! launchctl print "${domain}/com.${service_prefix}.${stopping_label}" >/dev/null 2>&1; then
      stopped_labels+=("${stopping_label}")
    fi
    if [ "${agents_recorded}" = true ]; then
      for label in serve refresh generate catalog-publish notifications scheduler; do
        stop_agent_for_restore "${label}"
      done
    fi
    # A fresh install has no previous version for an agent it could not stop;
    # leave that agent its runtime rather than a missing one.
    if [ "${runtime_activated}" = true ] && [ "${venv_before}" = absent ] && \
        [ "${#still_running[@]}" -gt 0 ]; then
      runtime_restored=false
    elif [ "${runtime_activated}" = true ] && ! restore_venv; then
      runtime_restored=false
      quoted_new_runtime=$(printf '%q' "${new_runtime}")
      echo "Could not switch back to the previous version; the new one is kept in ${quoted_new_runtime}." >&2
      if [ "${venv_before}" = directory ] && [ -d "${legacy_runtime}/.venv" ]; then
        quoted_legacy_runtime=$(printf '%q' "${legacy_runtime}")
        echo "The previous version is kept in ${quoted_legacy_runtime}." >&2
      fi
    fi
    if [ "${agents_recorded}" = true ]; then
      for label in serve refresh generate catalog-publish notifications scheduler; do
        restore_agent "${label}"
      done
    fi
    # An agent this run started and could not stop still uses the new runtime.
    if [ -n "${new_runtime}" ] && [ "${runtime_restored}" = true ] && \
        [ "${#still_running[@]}" -eq 0 ] && [ "${new_runtime_linked}" != true ] && \
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
    if [ "${#still_running[@]}" -gt 0 ]; then
      echo "The upgrade failed and these agents could not be stopped: ${still_running[*]}." >&2
      if [ "${runtime_activated}" = true ]; then
        quoted_new_runtime=$(printf '%q' "${new_runtime}")
        echo "They may still run the new version, kept in ${quoted_new_runtime}." >&2
      fi
      echo "Stop each with: launchctl bootout ${domain}/<agent>" >&2
    fi
    if [ "${#unrestored[@]}" -gt 0 ]; then
      echo "The upgrade failed and these agents are not running: ${unrestored[*]}." >&2
      quoted_agents_dir=$(printf '%q' "${agents_dir}")
      echo "Start each with: launchctl bootstrap ${domain} ${quoted_agents_dir}/<agent>.plist" >&2
    elif [ "${runtime_restored}" = true ] && [ "${#still_running[@]}" -eq 0 ]; then
      if [ "${#stopped_labels[@]}" -gt 0 ]; then
        echo "The upgrade failed; the agents it stopped were started again on the previous version." >&2
      elif [ "${runtime_activated}" = true ] && [ "${venv_before}" != absent ]; then
        echo "The upgrade failed; the previous version is running again." >&2
      fi
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
ready_dir=""
if [ -z "${service_account}" ]; then
  make_temp_dir work_dir "${support_dir}/install.XXXXXX"
  ready_dir=${work_dir}
else
  # Root's own folder in the install root, which only root can change. The
  # writer lock holder, which runs as the account, says in its folder there
  # that it holds the locks.
  make_temp_dir work_dir "${install_root}/install.XXXXXX"
  recovery_parent=${work_dir}
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
fi
plist_backup_dir="${work_dir}/launchd"
mkdir "${plist_backup_dir}"
if [ -n "${service_account}" ]; then
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
  runtimes_dir=$(cd -P "${runtimes_dir}" && pwd)
fi

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
# A stray UV_PROJECT_ENVIRONMENT would build the venv somewhere the agents do not run.
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

for label in serve refresh generate catalog-publish notifications scheduler; do
  if launchctl print "${domain}/com.${service_prefix}.${label}" >/dev/null 2>&1; then
    previously_loaded_labels+=("${label}")
  fi
  plist="${agents_dir}/com.${service_prefix}.${label}.plist"
  if [ -f "${plist}" ]; then
    cp "${plist}" "${plist_backup_dir}/"
  fi
done
agents_recorded=true

# A first install's jobs have no runtime until the switch: write them aside
# and move them in after it, so a power loss in between leaves launchd no job
# to start at boot or login from a missing runtime.
first_install=false
if [ ! -e "${app_dir}/.venv" ] && [ ! -L "${app_dir}/.venv" ]; then
  first_install=true
  mkdir "${work_dir}/agents"
  place_agents "${work_dir}/agents"
fi

# Check the configuration, sync the catalog and write the agents, with the
# interpreter and arguments given. A system service runs it twice: as its
# account ("sync"), then as root ("render").
controller_program() {
  "$@" <<'PY'
import plistlib
import os
import sys
from contextlib import nullcontext
from pathlib import Path

from inky_bird_frame.catalog import catalog_state_lock
from inky_bird_frame.config import DiscoveryProvider, load_config
from inky_bird_frame.errors import ConfigurationError
from inky_bird_frame.http import write_bytes_atomic
from inky_bird_frame.installation import controller_executable_path
from inky_bird_frame.installation import native_oidc_environment, native_tls_environment
from inky_bird_frame.publisher import sync_public_catalog

(
    serve_path,
    refresh_path,
    generation_path,
    catalog_publish_path,
    notifications_path,
    scheduler_path,
    root,
    app_dir,
    config_path,
    log_dir,
) = map(Path, sys.argv[1:11])
# The installer holds every writer lock, the catalog's among them.
writer_locks_held = sys.argv[11] == "true"
# A system service runs this twice: as its account to check the configuration
# and sync the catalog ("sync"), then as root, which reads no secret, to write
# its LaunchDaemons ("render").
mode = sys.argv[12] if len(sys.argv) > 12 else ""
if mode == "render":
    from inky_bird_frame.installation import read_account_config, system_service_problem

    account, group, environment_file, home = sys.argv[13:17]
    config = read_account_config(config_path, int(sys.argv[17]))
    # Other installations' web services on this machine, by their own jobs.
    others = []
    for path in sorted(Path(os.environ["INKY_BIRD_JOBS_DIR"]).glob("com.*.serve.plist")):
        try:
            with path.open("rb") as handle:
                job = plistlib.load(handle)
            other = job["UserName"]
            arguments = job["ProgramArguments"]
            other_config = Path(arguments[arguments.index("--config") + 1])
            if other != account and path.name == f"com.{other}.serve.plist":
                import pwd

                owner = pwd.getpwnam(other).pw_uid
                others.append((other, read_account_config(other_config, owner)))
        except (KeyError, ValueError, OSError, plistlib.InvalidFileException, ConfigurationError):
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
        f"The macOS LaunchAgent installer cannot use {names}; store the corresponding "
        "credential directly in the private config file"
    )
environment_destinations = [
    destination.name
    for destination in config.notifications.destinations
    if config.notifications.enabled and destination.url_env is not None
]
if environment_destinations and not mode:
    names = ", ".join(environment_destinations)
    raise ConfigurationError(
        "The macOS LaunchAgent installer requires direct notification URLs in the "
        f"private config file; replace url_env for: {names}"
    )
# A system service's units carry no secret: the application loads the
# private environment file itself.
owner_environment = native_oidc_environment(config, config_path) if not mode else {}
tls_environment = native_tls_environment(config_path) if not mode else {}
schedule = config.schedule
if mode != "render":
    config.controller.workspace_dir.mkdir(parents=True, exist_ok=True)
    config.controller.catalog_dir.parent.mkdir(parents=True, exist_ok=True)
    with nullcontext() if writer_locks_held else catalog_state_lock(config.controller.state_dir):
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
common = {
    "WorkingDirectory": str(app_dir),
    "ProcessType": "Background",
    "Umask": 0o077,
}
system_environment = {}
if mode == "render":
    # A LaunchDaemon run as the account, with its groups; its definition holds
    # no secret, so anyone may read it, doctor included.
    common.update(UserName=account, GroupName=group, InitGroups=True)
    system_environment = {"HOME": home, "INKY_BIRD_ENV_FILE": environment_file}
plist_mode = 0o644 if mode == "render" else 0o600


def managed_agent(name: str) -> dict[str, object]:
    return {
        **common,
        "EnvironmentVariables": {
            "PATH": controller_executable_path(
                config.controller.codex_path,
                generation_enabled=config.controller.generation_enabled,
            ),
            "PYTHONUNBUFFERED": "1",
            "INKY_BIRD_MANAGED_LOG_PATH": str(log_dir / f"{name}.log"),
            **tls_environment,
            **(owner_environment if name == "serve" else {}),
            **system_environment,
        },
    }


serve = {
    **managed_agent("serve"),
    "Label": f"com.{service_prefix}.serve",
    "ProgramArguments": [str(executable), "serve", "--config", str(config_path)],
    "RunAtLoad": True,
    "KeepAlive": {"SuccessfulExit": False},
    "ThrottleInterval": 10,
    "StandardOutPath": str(log_dir / "serve.log"),
    "StandardErrorPath": str(log_dir / "serve.error.log"),
}
refresh = {
    **managed_agent("refresh"),
    "Label": f"com.{service_prefix}.refresh",
    "ProgramArguments": [str(executable), "refresh", "--config", str(config_path)],
    "RunAtLoad": True,
    "StartInterval": schedule.refresh_minutes * 60,
    "StandardOutPath": str(log_dir / "refresh.log"),
    "StandardErrorPath": str(log_dir / "refresh.error.log"),
}
generation = {
    **managed_agent("generate"),
    "Label": f"com.{service_prefix}.generate",
    "ProgramArguments": [str(executable), "generate", "--config", str(config_path)],
    "StartInterval": schedule.generation_minutes * 60,
    "StandardOutPath": str(log_dir / "generate.log"),
    "StandardErrorPath": str(log_dir / "generate.error.log"),
}
catalog_publish = {
    **managed_agent("catalog-publish"),
    "Label": f"com.{service_prefix}.catalog-publish",
    "ProgramArguments": [
        str(executable),
        "catalog-publish",
        "--config",
        str(config_path),
    ],
    "RunAtLoad": True,
    "StartInterval": schedule.catalog_publish_minutes * 60,
    "StandardOutPath": str(log_dir / "catalog-publish.log"),
    "StandardErrorPath": str(log_dir / "catalog-publish.error.log"),
}
notifications = {
    **managed_agent("notifications"),
    "Label": f"com.{service_prefix}.notifications",
    "ProgramArguments": [
        str(executable),
        "notifications",
        "dispatch",
        "--config",
        str(config_path),
    ],
    "RunAtLoad": True,
    "StartInterval": config.notifications.delivery_retry_minutes * 60,
    "StandardOutPath": str(log_dir / "notifications.log"),
    "StandardErrorPath": str(log_dir / "notifications.error.log"),
}
for path, payload in (
    (serve_path, serve),
    (refresh_path, refresh),
):
    write_bytes_atomic(path, plistlib.dumps(payload, sort_keys=True), mode=plist_mode)
if config.controller.generation_enabled:
    write_bytes_atomic(generation_path, plistlib.dumps(generation, sort_keys=True), mode=plist_mode)
else:
    generation_path.unlink(missing_ok=True)
if config.public_catalog.enabled:
    write_bytes_atomic(catalog_publish_path, plistlib.dumps(catalog_publish, sort_keys=True), mode=plist_mode)
else:
    catalog_publish_path.unlink(missing_ok=True)
if config.notifications.enabled:
    write_bytes_atomic(notifications_path, plistlib.dumps(notifications, sort_keys=True), mode=plist_mode)
else:
    notifications_path.unlink(missing_ok=True)
if config.controller.configuration_ownership == "app":
    scheduler = {
        **managed_agent("scheduler"),
        "Label": f"com.{service_prefix}.scheduler",
        "ProgramArguments": [str(executable), "scheduler", "--config", str(config_path)],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "StandardOutPath": str(log_dir / "scheduler.log"),
        "StandardErrorPath": str(log_dir / "scheduler.error.log"),
    }
    write_bytes_atomic(scheduler_path, plistlib.dumps(scheduler, sort_keys=True), mode=plist_mode)
    for path in (refresh_path, generation_path, catalog_publish_path, notifications_path):
        path.unlink(missing_ok=True)
else:
    scheduler_path.unlink(missing_ok=True)
PY
}

if [ -z "${service_account}" ]; then
  controller_program "${new_runtime}/.venv/bin/python" - \
    "${serve_plist}" "${refresh_plist}" "${generation_plist}" "${catalog_publish_plist}" \
    "${notifications_plist}" "${scheduler_plist}" \
    "${catalog_root}" "${app_dir}" "${config_path}" "${log_dir}" "${writer_locks_held}"
else
  # Root copies the public catalog into the code it keeps; the account syncs
  # its own catalog from there.
  if [ "${catalog_root}" != "${app_dir}" ]; then
    contained "${app_dir}" "${app_dir}/catalog"
    rsync -a --delete "${copy_options[@]}" "${catalog_root}/catalog/" "${app_dir}/catalog/"
  fi
  for program_mode in sync render; do
    runner=()
    if [ "${program_mode}" = sync ]; then
      runner=("${account_prefix[@]}")
    fi
    controller_program ${runner[@]+"${runner[@]}"} "${new_runtime}/.venv/bin/python" - \
      "${serve_plist}" "${refresh_plist}" "${generation_plist}" "${catalog_publish_plist}" \
      "${notifications_plist}" "${scheduler_plist}" \
      "${app_dir}" "${app_dir}" "${config_path}" "${log_dir}" "${writer_locks_held}" \
      "${program_mode}" "${service_account}" "${account_group}" "${environment_file}" \
      "${account_home}" "${account_uid}"
  done
fi

if [ -f "${catalog_publish_plist}" ]; then
  if launchctl print "${domain}/com.${service_prefix}.catalog-publish" >/dev/null 2>&1; then
    stopping_label=catalog-publish
    launchctl bootout "${domain}/com.${service_prefix}.catalog-publish"
    stopped_labels+=("catalog-publish")
    stopping_label=""
  fi
  # The check takes the publisher's own locks.
  release_writer_locks
  # A failure here puts the publisher back, without RunAtLoad, on exit.
  as_account "${new_runtime}/.venv/bin/inky-bird" catalog-publish --config "${config_path}" --dry-run
  hold_writer_locks true
fi

for label in serve refresh generate catalog-publish notifications scheduler; do
  if launchctl print "${domain}/com.${service_prefix}.${label}" >/dev/null 2>&1; then
    stopping_label=${label}
    launchctl bootout "${domain}/com.${service_prefix}.${label}"
    stopped_labels+=("${label}")
    stopping_label=""
  fi
done
if [ -n "${stopped_hook}" ] && [ -n "${service_account}" ] && ! account_processes_gone; then
  echo "Processes of ${service_account} still run after its services stopped; the upgrade did not switch to the new version." >&2
  exit 1
fi
run_stopped_hook
release_writer_locks
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
if [ "${first_install}" = true ]; then
  for name in "${agent_plists[@]}"; do
    staged=${!name}
    if [ -f "${staged}" ]; then
      mv "${staged}" "${agents_dir}/${staged##*/}"
    fi
  done
  place_agents "${agents_dir}"
fi
launchctl bootstrap "${domain}" "${serve_plist}"
if [ -f "${refresh_plist}" ]; then
  launchctl bootstrap "${domain}" "${refresh_plist}"
fi
if [ -f "${scheduler_plist}" ]; then
  launchctl bootstrap "${domain}" "${scheduler_plist}"
fi
if [ -f "${generation_plist}" ]; then
  launchctl bootstrap "${domain}" "${generation_plist}"
fi
if [ -f "${catalog_publish_plist}" ]; then
  launchctl bootstrap "${domain}" "${catalog_publish_plist}"
fi
if [ -f "${notifications_plist}" ]; then
  launchctl bootstrap "${domain}" "${notifications_plist}"
fi
launchctl print "${domain}/com.${service_prefix}.serve" >/dev/null
if [ -f "${refresh_plist}" ]; then
  launchctl print "${domain}/com.${service_prefix}.refresh" >/dev/null
fi
if [ -f "${scheduler_plist}" ]; then
  launchctl print "${domain}/com.${service_prefix}.scheduler" >/dev/null
fi
if [ -f "${generation_plist}" ]; then
  launchctl print "${domain}/com.${service_prefix}.generate" >/dev/null
fi
if [ -f "${catalog_publish_plist}" ]; then
  launchctl print "${domain}/com.${service_prefix}.catalog-publish" >/dev/null
fi
if [ -f "${notifications_plist}" ]; then
  launchctl print "${domain}/com.${service_prefix}.notifications" >/dev/null
fi
if [ -f "${serve_plist}" ]; then
  check_new_serve
fi

installation_complete=true
# The managed directory's copy of this script reinstalls the active runtime;
# the mark tells it apart from a source checkout used as that directory.
if [ ! "${root}/deploy" -ef "${app_dir}/deploy" ]; then
  install -m 0755 "${root}/deploy/install-controller.sh" "${app_dir}/deploy/"
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
# Remove the legacy single-agent layout only after the new agents verified,
# so a failed upgrade leaves the previous controller runnable.
if launchctl print "${domain}/com.${service_prefix}.controller-cycle" >/dev/null 2>&1; then
  launchctl bootout "${domain}/com.${service_prefix}.controller-cycle"
fi
rm -f "${legacy_cycle_plist}"
quoted_root=$(printf '%q' "${root}")
quoted_app_dir=$(printf '%q' "${app_dir}")
echo "Controller installed from ${quoted_root} into ${quoted_app_dir}."
quoted_config_path=$(printf '%q' "${config_path}")
echo "Configuration: ${quoted_config_path}"
quoted_log_dir=$(printf '%q' "${log_dir}")
echo "Logs: ${quoted_log_dir}"
if [ -n "${previous_runtime}" ]; then
  quoted_previous_runtime=$(printf '%q' "${previous_runtime}")
  echo "Previous version kept for rollback: ${quoted_previous_runtime}"
fi
if ! as_account "${app_dir}/.venv/bin/inky-bird" access setup --if-needed --config "${config_path}"; then
  echo "Controller installation completed; owner setup is incomplete. Run access setup in a private terminal. Display and scheduled work continue." >&2
fi
