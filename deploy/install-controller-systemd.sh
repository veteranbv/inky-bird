#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != "Linux" ] || ! command -v systemctl >/dev/null 2>&1; then
  echo "Controller installer requires Linux with systemd." >&2
  exit 1
fi
if [ "$(id -u)" = 0 ]; then
  echo "Run the installer as the account that will own the controller, not as root. No changes made." >&2
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
native_names=$(python3 "${root}/src/inky_bird_frame/native_names.py")
service_prefix="" app_dir="" support_dir="" config_path=""
eval "${native_names}"
export INKY_BIRD_SERVICE_PREFIX="${service_prefix}"
python_version=${INKY_BIRD_PYTHON_VERSION:-3.11}
quiesce_timeout_seconds=1800
poll_seconds=1
uv_bin=${UV_BIN:-}
if [ -z "${uv_bin}" ]; then
  uv_bin=$(command -v uv || true)
fi
unit_root="/etc/systemd/system"
# Each runtime holds one source copy and the venv built from it;
# ${app_dir}/.venv links to the active one.
runtimes_dir="${app_dir}/.runtimes"
# setup --yes from the installed CLI runs the active runtime's own copy of this
# script. A runtime holds no catalog; that stays in the managed directory.
catalog_root=${root}
if [ "$(cd -P "${root}/.." && pwd)" = "$(cd -P "${runtimes_dir}" 2>/dev/null && pwd || true)" ]; then
  catalog_root=${app_dir}
fi
user=$(id -un)
managed_units=()
for name in controller scheduler refresh generate catalog-publish notifications; do
  managed_units+=("${service_prefix}-${name}.service" "${service_prefix}-${name}.timer")
done

# Run by the installed version's Python: hold its controller cycle lock so no
# plate is being made while services stop. Exit 3 if a cycle is still running
# at the deadline. If the installed code cannot be loaded or cannot read the
# config, the lock cannot be found: say so and exit 0 without waiting, so a
# broken or older install can still be upgraded or repaired.
cycle_lock_program='
import fcntl, os, sys, time
from pathlib import Path
config_path, ready, timeout = Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3])
try:
    from inky_bird_frame.config import load_config
    try:
        config = load_config(config_path, load_secrets=False)
    except TypeError:
        config = load_config(config_path)
    state_dir = config.controller.state_dir
except Exception:
    print(
        "The installed version cannot be loaded with this configuration, so the "
        "upgrade cannot check for a plate being made.",
        file=sys.stderr,
    )
    sys.exit(0)
state_dir.mkdir(parents=True, exist_ok=True)
handle = (state_dir / "controller.lock").open("a+")
deadline = time.monotonic() + timeout
announced = False
while True:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except BlockingIOError:
        if time.monotonic() >= deadline:
            sys.exit(3)
        if not announced:
            print("Waiting for the plate being made to finish.", file=sys.stderr)
            announced = True
        time.sleep(2)
ready.touch()
parent = os.getppid()
while os.getppid() == parent:
    time.sleep(1)
'

if [ ! -f "${config_path}" ]; then
  echo "Controller configuration is missing: ${config_path}" >&2
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
    if [ "${owner}" != "${user}" ]; then
      echo "${service_prefix}-${name}.service runs as ${owner}. Run the installer as ${owner}. No changes made." >&2
      exit 1
    fi
  fi
done
if ! sudo -v; then
  echo "Administrator access is required to install system services." >&2
  exit 1
fi

work_dir=""
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
cycle_lock_pid=""
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

# Copy what building, and later reinstalling, a runtime needs from ${root}.
stage_runtime_source() {
  local target=$1
  rsync -a --delete "${root}/src/" "${target}/src/"
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

hold_cycle_lock() {
  local python="${app_dir}/.venv/bin/python"
  local ready="${work_dir}/cycle-lock-held"
  local status=0
  if [ ! -x "${python}" ]; then
    return 0
  fi
  "${python}" -I -c "${cycle_lock_program}" "${config_path}" "${ready}" \
    "${quiesce_timeout_seconds}" &
  cycle_lock_pid=$!
  until [ -e "${ready}" ]; do
    if ! kill -0 "${cycle_lock_pid}" 2>/dev/null; then
      wait "${cycle_lock_pid}" || status=$?
      cycle_lock_pid=""
      if [ "${status}" -eq 0 ]; then
        return 0
      fi
      if [ "${status}" -eq 3 ]; then
        echo "A plate is still being made. Run the upgrade again after it finishes. No changes made." >&2
        exit 3
      fi
      echo "Could not check whether a plate is being made. No changes made." >&2
      exit 1
    fi
    sleep "${poll_seconds}"
  done
}

release_cycle_lock() {
  if [ -n "${cycle_lock_pid}" ]; then
    kill "${cycle_lock_pid}" 2>/dev/null || true
    wait "${cycle_lock_pid}" 2>/dev/null || true
    cycle_lock_pid=""
  fi
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

replace_venv_link() {
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

# The previous version cannot run from ${app_dir}/.venv any more: restart what
# still runs it so every service uses the version that is there, and say so.
restart_on_new_runtime() {
  local unit
  local restarted=()
  for unit in ${active_before[@]+"${active_before[@]}"}; do
    if [ -e "${unit_root}/${unit}" ] && systemctl is-active --quiet "${unit}" && \
        ! contains "${unit}" ${started_units[@]+"${started_units[@]}"}; then
      if sudo systemctl restart "${unit}"; then
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
          sudo install -m 0644 "${unit_backup_dir}/${unit}" "${unit_root}/${unit}" || true
        fi
      elif [ -e "${unit_root}/${unit}" ]; then
        changed=true
        if { running "${unit}" || systemctl is-enabled --quiet "${unit}"; } && \
            ! sudo systemctl disable --now "${unit}"; then
          # Keep its file so a rerun or the uninstaller can still find and stop it.
          stuck_units+=("${unit}")
          continue
        fi
        sudo rm -f "${unit_root}/${unit}" || true
      fi
    done
    if [ "${changed}" = true ]; then
      sudo systemctl daemon-reload || true
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
            sudo systemctl stop "${unit}" || stuck_units+=("${unit}")
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
      echo "Could not switch back to the previous version; the new one stays active, kept in ${new_runtime}." >&2
      if [ "${venv_before}" = directory ] && [ -d "${legacy_runtime}/.venv" ]; then
        echo "The previous version is kept in ${legacy_runtime}." >&2
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
        sudo systemctl enable "${unit}" || true
    elif systemctl is-enabled --quiet "${unit}"; then
      sudo systemctl disable "${unit}" || true
    fi
    if long_running "${unit}"; then
      if contains "${unit}" ${active_before[@]+"${active_before[@]}"}; then
        [ "${startable}" = false ] || running "${unit}" || \
          sudo systemctl start "${unit}" || true
      elif running "${unit}"; then
        sudo systemctl stop "${unit}" || true
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
    echo "These services could not be stopped or disabled and may still run the new version, kept in ${new_runtime}: ${stuck_units[*]}." >&2
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
  release_cycle_lock
  if [ "${installation_complete}" != true ]; then
    restore_units
    report_restore
    if [ -n "${new_runtime}" ] && [ "${#stuck_units[@]}" -eq 0 ] && \
        [ "${venv_restored}" = true ] && [ "${new_runtime_linked}" != true ] && \
        ! rm -rf "${new_runtime}"; then
      leftovers+=("${new_runtime}")
    fi
    if [ "${#leftovers[@]}" -gt 0 ]; then
      echo "Could not remove what this upgrade added; nothing uses it, so remove it by hand: ${leftovers[*]}" >&2
    fi
    if [ "${created_runtimes_dir}" = true ]; then
      rmdir "${runtimes_dir}" 2>/dev/null || true
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
make_temp_dir work_dir
unit_dir="${work_dir}/units"
unit_backup_dir="${work_dir}/previous-units"
mkdir "${unit_dir}" "${unit_backup_dir}"

chmod 600 "${config_path}"
if [ ! -d "${runtimes_dir}" ]; then
  created_runtimes_dir=true
fi
mkdir -p "${app_dir}/catalog" "${app_dir}/deploy" "${runtimes_dir}" "${support_dir}"
runtimes_dir=$(cd -P "${runtimes_dir}" && pwd)

# Build and check the new runtime beside the live one; nothing running changes.
make_temp_dir new_runtime "${runtimes_dir}/runtime.XXXXXX"
stage_runtime_source "${new_runtime}"
# A stray UV_PROJECT_ENVIRONMENT would build the venv somewhere the units do not run.
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

hold_cycle_lock
# Waiting for a plate can outlast cached sudo credentials; ask again before stopping anything.
if ! sudo -v; then
  echo "Administrator access is required to install system services. No changes made." >&2
  exit 1
fi
record_units
# App-owned installations use the same dynamic scheduler as Docker. Stop it
# before replacing its runtime; a managed installation removes it below.
if systemctl is-active --quiet "${service_prefix}-scheduler.service"; then
  stopped_units+=("${service_prefix}-scheduler.service")
  sudo systemctl stop "${service_prefix}-scheduler.service"
fi
for name in refresh generate catalog-publish notifications; do
  timer="${service_prefix}-${name}.timer"
  if systemctl is-active --quiet "${timer}"; then
    stopped_units+=("${timer}")
    sudo systemctl stop "${timer}"
  fi
done
deadline=$((SECONDS + quiesce_timeout_seconds))
for name in refresh generate catalog-publish notifications; do
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
release_cycle_lock
# The wait above can also outlast cached credentials; the steps below need them.
if ! sudo -v; then
  echo "Administrator access is required to install system services." >&2
  exit 1
fi

"${new_runtime}/.venv/bin/python" - \
  "${unit_dir}" "${catalog_root}" "${app_dir}" "${config_path}" "${HOME}" "${user}" <<'PY'
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
        f"The systemd installer cannot use {names}; store the corresponding credential "
        "directly in the private config file"
    )
environment_destinations = [
    destination.name
    for destination in config.notifications.destinations
    if config.notifications.enabled and destination.url_env is not None
]
if environment_destinations:
    names = ", ".join(environment_destinations)
    raise ConfigurationError(
        "The systemd installer requires direct notification URLs in the private "
        f"config file; replace url_env for: {names}"
    )
native_oidc_environment(config, config_path)
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

service_prefix = os.environ["INKY_BIRD_SERVICE_PREFIX"]
# The command named like the services: 0.9 installs had only inky-bird-frame,
# so a service whose definition a failed upgrade could not put back still starts.
executable = app_dir / ".venv/bin" / service_prefix
units = controller_systemd_units(
    config,
    executable=executable,
    app_dir=app_dir,
    config_path=config_path,
    home=home,
    user=user,
    service_prefix=service_prefix,
)
for name, content in units.items():
    (unit_dir / name).write_text(content)
PY

# Only an unreachable bird source may wait for the scheduled refresh; any other
# failure means the new version does not work here.
refresh_status=0
refresh_output=$("${new_runtime}/.venv/bin/inky-bird" refresh --config "${config_path}") || \
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
  "${new_runtime}/.venv/bin/inky-bird" catalog-publish \
    --config "${config_path}" --dry-run
fi

activate_runtime
units_replaced=true
for unit in "${unit_dir}"/*; do
  sudo install -m 0644 "${unit}" "${unit_root}/$(basename "${unit}")"
done
for optional in refresh generate catalog-publish notifications; do
  if [ ! -f "${unit_dir}/${service_prefix}-${optional}.timer" ]; then
    if [ -e "${unit_root}/${service_prefix}-${optional}.timer" ]; then
      sudo systemctl disable --now "${service_prefix}-${optional}.timer"
    fi
    sudo rm -f \
      "${unit_root}/${service_prefix}-${optional}.service" \
      "${unit_root}/${service_prefix}-${optional}.timer"
  fi
done
if [ ! -f "${unit_dir}/${service_prefix}-scheduler.service" ]; then
  if [ -e "${unit_root}/${service_prefix}-scheduler.service" ]; then
    sudo systemctl disable --now "${service_prefix}-scheduler.service"
  fi
  sudo rm -f "${unit_root}/${service_prefix}-scheduler.service"
fi

sudo systemctl daemon-reload
sudo systemctl enable "${service_prefix}-controller.service"
if [ -f "${unit_dir}/${service_prefix}-refresh.timer" ]; then
  sudo systemctl enable "${service_prefix}-refresh.timer"
fi
if [ -f "${unit_dir}/${service_prefix}-scheduler.service" ]; then
  sudo systemctl enable "${service_prefix}-scheduler.service"
fi
if [ -f "${unit_dir}/${service_prefix}-generate.timer" ]; then
  sudo systemctl enable "${service_prefix}-generate.timer"
fi
if [ -f "${unit_dir}/${service_prefix}-catalog-publish.timer" ]; then
  sudo systemctl enable "${service_prefix}-catalog-publish.timer"
fi
if [ -f "${unit_dir}/${service_prefix}-notifications.timer" ]; then
  sudo systemctl enable "${service_prefix}-notifications.timer"
fi
started_units+=("${service_prefix}-controller.service")
sudo systemctl restart "${service_prefix}-controller.service"
if [ -f "${unit_dir}/${service_prefix}-refresh.timer" ]; then
  sudo systemctl restart "${service_prefix}-refresh.timer"
fi
if [ -f "${unit_dir}/${service_prefix}-scheduler.service" ]; then
  started_units+=("${service_prefix}-scheduler.service")
  sudo systemctl restart "${service_prefix}-scheduler.service"
fi
if [ -f "${unit_dir}/${service_prefix}-generate.timer" ]; then
  sudo systemctl restart "${service_prefix}-generate.timer"
fi
if [ -f "${unit_dir}/${service_prefix}-catalog-publish.timer" ]; then
  sudo systemctl restart "${service_prefix}-catalog-publish.timer"
fi
if [ -f "${unit_dir}/${service_prefix}-notifications.timer" ]; then
  sudo systemctl restart "${service_prefix}-notifications.timer"
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
    echo "The runtime this installer ran from is kept in ${runtime}." >&2
  elif ! rm -rf "${runtime}"; then
    echo "Could not remove an older runtime; remove it by hand: ${runtime}" >&2
  fi
done
echo "Controller installed from ${root} into ${app_dir}."
echo "Configuration: ${config_path}"
if [ -n "${previous_runtime}" ]; then
  echo "Previous version kept for rollback: ${previous_runtime}"
fi
if ! "${app_dir}/.venv/bin/inky-bird" access setup --if-needed --config "${config_path}"; then
  echo "Controller installation completed; owner setup is incomplete. Run access setup in a private terminal. Display and scheduled work continue." >&2
fi
