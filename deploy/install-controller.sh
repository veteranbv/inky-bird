#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
  echo "Controller installer currently supports macOS." >&2
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
    system_plist="/Library/LaunchDaemons/com.${family}.${label}.plist"
    if [ -e "${system_plist}" ] || [ -L "${system_plist}" ] || \
        launchctl print "system/com.${family}.${label}" >/dev/null 2>&1; then
      echo "Inky uses operator-managed system services. Use that deployment process to upgrade; the user installer made no changes." >&2
      exit 1
    fi
  done
done
stopped_hook=""
stopped_hook_timeout=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --stopped-hook | --stopped-hook-timeout)
      if [ "$#" -lt 2 ]; then
        echo "$1 needs a value. No changes made." >&2
        exit 1
      fi
      if [ "$1" = --stopped-hook ]; then
        stopped_hook=$2
      else
        stopped_hook_timeout=$2
      fi
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

native_names=$(python3 "${root}/src/inky_bird_frame/native_names.py")
service_prefix="" app_dir="" support_dir="" config_path=""
eval "${native_names}"
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
serve_plist="${agents_dir}/com.${service_prefix}.serve.plist"
refresh_plist="${agents_dir}/com.${service_prefix}.refresh.plist"
generation_plist="${agents_dir}/com.${service_prefix}.generate.plist"
catalog_publish_plist="${agents_dir}/com.${service_prefix}.catalog-publish.plist"
notifications_plist="${agents_dir}/com.${service_prefix}.notifications.plist"
scheduler_plist="${agents_dir}/com.${service_prefix}.scheduler.plist"
legacy_cycle_plist="${agents_dir}/com.${service_prefix}.controller-cycle.plist"
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
  recovery_dir=$(mktemp -d "${support_dir}/catalog-publish-recovery.XXXXXX") || return 1
  recovery_plist="${recovery_dir}/catalog-publish.plist"
  cp "${catalog_publish_plist}" "${recovery_plist}" && \
    /usr/bin/plutil -replace RunAtLoad -bool false "${recovery_plist}" && \
    launchctl bootstrap "gui/${uid}" "${recovery_plist}" || status=1
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
  rsync -a --delete "${root}/src/" "${target}/src/"
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

if [ ! -f "${config_path}" ]; then
  echo "Controller configuration is missing: ${config_path}" >&2
  exit 1
fi
chmod 600 "${config_path}"
if [ -z "${uv_bin}" ] || [ ! -x "${uv_bin}" ]; then
  echo "uv is not executable. Install uv or set UV_BIN." >&2
  exit 1
fi

created_runtimes_dir=false
if [ ! -d "${runtimes_dir}" ]; then
  created_runtimes_dir=true
fi
mkdir -p "${app_dir}/catalog" "${app_dir}/deploy" "${runtimes_dir}" "${support_dir}" \
  "${log_dir}" "${agents_dir}"
chmod 700 "${support_dir}" "${log_dir}"
find "${log_dir}" -maxdepth 1 -type f -name '*.log*' -exec chmod 600 {} +
runtimes_dir=$(cd -P "${runtimes_dir}" && pwd)
uid=$(id -u)
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
  ready="${work_dir}/writer-locks-held.${writer_lock_holds}"
  if [ ! -x "${python}" ] || [ "${writer_locks_unavailable}" = true ]; then
    return 0
  fi
  start_background writer_lock_pid "${python}" -I \
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
  echo "Running the stopped-boundary hook: ${stopped_hook}" >&2
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
  if ! run_bounded "$((health_timeout_seconds + 15))" "${new_runtime}/.venv/bin/python" -I \
      -m inky_bird_frame.upgrade_gates check-serve "${config_path}" \
      "${health_timeout_seconds}"; then
    echo "The new version started, but its web service failed its health check." >&2
    exit 1
  fi
}

replace_venv_link() {
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
    previous_runtime=${legacy_runtime}
    mv "${app_dir}/.venv" "${legacy_runtime}/.venv"
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
          rm -f "${app_dir}/.venv" || return 1
        fi
        if ! mv "${legacy_runtime}/.venv" "${app_dir}/.venv"; then
          # Leave the services the new version rather than nothing at all.
          replace_venv_link "${new_runtime}/.venv"
          return 1
        fi
      fi
      if [ -n "${legacy_runtime}" ] && ! rmdir "${legacy_runtime}"; then
        leftovers+=("${legacy_runtime}")
      fi
      ;;
    *)
      # Nothing ran before this run, so nothing uses the link it made.
      if ! rm -f "${app_dir}/.venv"; then
        leftovers+=("${app_dir}/.venv" "${new_runtime}")
        new_runtime_linked=true
      fi
      ;;
  esac
  remove_next_link
}

remove_next_link() {
  if [ -e "${app_dir}/.venv.next" ] || [ -L "${app_dir}/.venv.next" ]; then
    rm -f "${app_dir}/.venv.next" || leftovers+=("${app_dir}/.venv.next")
  fi
}


# An agent this run stopped or loaded must not run while the runtime switches
# back; an agent it never touched keeps running.
stop_agent_for_restore() {
  local label=$1
  local agent="com.${service_prefix}.${label}"
  if { contains "${label}" ${stopped_labels[@]+"${stopped_labels[@]}"} || \
      ! contains "${label}" ${previously_loaded_labels[@]+"${previously_loaded_labels[@]}"}; } && \
      launchctl print "gui/${uid}/${agent}" >/dev/null 2>&1; then
    if ! launchctl bootout "gui/${uid}/${agent}"; then
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
    launchctl bootstrap "gui/${uid}" "${plist}" || unrestored+=("${agent}")
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
        ! launchctl print "gui/${uid}/com.${service_prefix}.${stopping_label}" >/dev/null 2>&1; then
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
      echo "Could not switch back to the previous version; the new one is kept in ${new_runtime}." >&2
      if [ "${venv_before}" = directory ] && [ -d "${legacy_runtime}/.venv" ]; then
        echo "The previous version is kept in ${legacy_runtime}." >&2
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
        ! rm -rf "${new_runtime}"; then
      leftovers+=("${new_runtime}")
    fi
    if [ "${#leftovers[@]}" -gt 0 ]; then
      echo "Could not remove what this upgrade added; nothing uses it, so remove it by hand: ${leftovers[*]}" >&2
    fi
    if [ "${created_runtimes_dir}" = true ]; then
      rmdir "${runtimes_dir}" 2>/dev/null || true
    fi
    if [ "${#still_running[@]}" -gt 0 ]; then
      echo "The upgrade failed and these agents could not be stopped: ${still_running[*]}." >&2
      if [ "${runtime_activated}" = true ]; then
        echo "They may still run the new version, kept in ${new_runtime}." >&2
      fi
      echo "Stop each with: launchctl bootout gui/${uid}/<agent>" >&2
    fi
    if [ "${#unrestored[@]}" -gt 0 ]; then
      echo "The upgrade failed and these agents are not running: ${unrestored[*]}." >&2
      echo "Start each with: launchctl bootstrap gui/${uid} ${agents_dir}/<agent>.plist" >&2
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
      echo "Previous version kept for rollback: ${previous_runtime}" >&2
    fi
  fi
  if [ -n "${work_dir}" ] && ! rm -rf "${work_dir}"; then
    echo "Could not remove temporary files; remove them by hand: ${work_dir}" >&2
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
make_temp_dir work_dir "${support_dir}/install.XXXXXX"
plist_backup_dir="${work_dir}/launchd"
mkdir "${plist_backup_dir}"

# Build and check the new runtime beside the live one; nothing running changes.
make_temp_dir new_runtime "${runtimes_dir}/runtime.XXXXXX"
stage_runtime_source "${new_runtime}"
# A stray UV_PROJECT_ENVIRONMENT would build the venv somewhere the agents do not run.
env -u UV_PROJECT_ENVIRONMENT "${uv_bin}" sync --project "${new_runtime}" \
  --python "${python_version}" --extra controller --locked
# The CLI reports a rejection as a JSON envelope on stdout; show its message.
if ! validation=$("${new_runtime}/.venv/bin/inky-bird" config validate --config "${config_path}"); then
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
if [ -n "${stopped_hook}" ] && ! stopped_hook_state_dir=$("${new_runtime}/.venv/bin/python" -I \
    -m inky_bird_frame.upgrade_gates state-dir "${config_path}"); then
  echo "Could not read the state directory for the stopped-boundary hook. No changes made." >&2
  exit 1
fi

hold_writer_locks false

for label in serve refresh generate catalog-publish notifications scheduler; do
  if launchctl print "gui/${uid}/com.${service_prefix}.${label}" >/dev/null 2>&1; then
    previously_loaded_labels+=("${label}")
  fi
  plist="${agents_dir}/com.${service_prefix}.${label}.plist"
  if [ -f "${plist}" ]; then
    cp "${plist}" "${plist_backup_dir}/"
  fi
done
agents_recorded=true

"${new_runtime}/.venv/bin/python" - \
  "${serve_plist}" "${refresh_plist}" "${generation_plist}" "${catalog_publish_plist}" \
  "${notifications_plist}" "${scheduler_plist}" \
  "${catalog_root}" "${app_dir}" "${config_path}" "${log_dir}" "${writer_locks_held}" <<'PY'
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
service_prefix = os.environ["INKY_BIRD_SERVICE_PREFIX"]
# The command named like the services: 0.9 installs had only inky-bird-frame,
# so a service whose definition a failed upgrade could not put back still starts.
executable = app_dir / ".venv/bin" / service_prefix
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
if environment_credentials:
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
if environment_destinations:
    names = ", ".join(environment_destinations)
    raise ConfigurationError(
        "The macOS LaunchAgent installer requires direct notification URLs in the "
        f"private config file; replace url_env for: {names}"
    )
owner_environment = native_oidc_environment(config, config_path)
tls_environment = native_tls_environment(config_path)
schedule = config.schedule
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

common = {
    "WorkingDirectory": str(app_dir),
    "ProcessType": "Background",
    "Umask": 0o077,
}


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
    write_bytes_atomic(path, plistlib.dumps(payload, sort_keys=True), mode=0o600)
if config.controller.generation_enabled:
    write_bytes_atomic(generation_path, plistlib.dumps(generation, sort_keys=True), mode=0o600)
else:
    generation_path.unlink(missing_ok=True)
if config.public_catalog.enabled:
    write_bytes_atomic(catalog_publish_path, plistlib.dumps(catalog_publish, sort_keys=True), mode=0o600)
else:
    catalog_publish_path.unlink(missing_ok=True)
if config.notifications.enabled:
    write_bytes_atomic(notifications_path, plistlib.dumps(notifications, sort_keys=True), mode=0o600)
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
    write_bytes_atomic(scheduler_path, plistlib.dumps(scheduler, sort_keys=True), mode=0o600)
    for path in (refresh_path, generation_path, catalog_publish_path, notifications_path):
        path.unlink(missing_ok=True)
else:
    scheduler_path.unlink(missing_ok=True)
PY

if [ -f "${catalog_publish_plist}" ]; then
  if launchctl print "gui/${uid}/com.${service_prefix}.catalog-publish" >/dev/null 2>&1; then
    stopping_label=catalog-publish
    launchctl bootout "gui/${uid}/com.${service_prefix}.catalog-publish"
    stopped_labels+=("catalog-publish")
    stopping_label=""
  fi
  # The check takes the publisher's own locks.
  release_writer_locks
  # A failure here puts the publisher back, without RunAtLoad, on exit.
  "${new_runtime}/.venv/bin/inky-bird" catalog-publish --config "${config_path}" --dry-run
  hold_writer_locks true
fi

for label in serve refresh generate catalog-publish notifications scheduler; do
  if launchctl print "gui/${uid}/com.${service_prefix}.${label}" >/dev/null 2>&1; then
    stopping_label=${label}
    launchctl bootout "gui/${uid}/com.${service_prefix}.${label}"
    stopped_labels+=("${label}")
    stopping_label=""
  fi
done
run_stopped_hook
release_writer_locks
activate_runtime
launchctl bootstrap "gui/${uid}" "${serve_plist}"
if [ -f "${refresh_plist}" ]; then
  launchctl bootstrap "gui/${uid}" "${refresh_plist}"
fi
if [ -f "${scheduler_plist}" ]; then
  launchctl bootstrap "gui/${uid}" "${scheduler_plist}"
fi
if [ -f "${generation_plist}" ]; then
  launchctl bootstrap "gui/${uid}" "${generation_plist}"
fi
if [ -f "${catalog_publish_plist}" ]; then
  launchctl bootstrap "gui/${uid}" "${catalog_publish_plist}"
fi
if [ -f "${notifications_plist}" ]; then
  launchctl bootstrap "gui/${uid}" "${notifications_plist}"
fi
launchctl print "gui/${uid}/com.${service_prefix}.serve" >/dev/null
if [ -f "${refresh_plist}" ]; then
  launchctl print "gui/${uid}/com.${service_prefix}.refresh" >/dev/null
fi
if [ -f "${scheduler_plist}" ]; then
  launchctl print "gui/${uid}/com.${service_prefix}.scheduler" >/dev/null
fi
if [ -f "${generation_plist}" ]; then
  launchctl print "gui/${uid}/com.${service_prefix}.generate" >/dev/null
fi
if [ -f "${catalog_publish_plist}" ]; then
  launchctl print "gui/${uid}/com.${service_prefix}.catalog-publish" >/dev/null
fi
if [ -f "${notifications_plist}" ]; then
  launchctl print "gui/${uid}/com.${service_prefix}.notifications" >/dev/null
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
    echo "The runtime this installer ran from is kept in ${runtime}." >&2
  elif ! rm -rf "${runtime}"; then
    echo "Could not remove an older runtime; remove it by hand: ${runtime}" >&2
  fi
done
# Remove the legacy single-agent layout only after the new agents verified,
# so a failed upgrade leaves the previous controller runnable.
if launchctl print "gui/${uid}/com.${service_prefix}.controller-cycle" >/dev/null 2>&1; then
  launchctl bootout "gui/${uid}/com.${service_prefix}.controller-cycle"
fi
rm -f "${legacy_cycle_plist}"
echo "Controller installed from ${root} into ${app_dir}."
echo "Configuration: ${config_path}"
echo "Logs: ${log_dir}"
if [ -n "${previous_runtime}" ]; then
  echo "Previous version kept for rollback: ${previous_runtime}"
fi
if ! "${app_dir}/.venv/bin/inky-bird" access setup --if-needed --config "${config_path}"; then
  echo "Controller installation completed; owner setup is incomplete. Run access setup in a private terminal. Display and scheduled work continue." >&2
fi
