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
native_names=$(python3 "${root}/src/inky_bird_frame/native_names.py")
service_prefix="" app_dir="" support_dir="" config_path=""
eval "${native_names}"
export INKY_BIRD_SERVICE_PREFIX="${service_prefix}"
python_version=${INKY_BIRD_PYTHON_VERSION:-3.11}
quiesce_timeout_seconds=1800
uv_bin=${UV_BIN:-}
if [ -z "${uv_bin}" ]; then
  uv_bin=$(command -v uv || true)
fi
unit_root="/etc/systemd/system"
runtimes_dir="${app_dir}/.runtimes"
user=$(id -un)

# Run by the old runtime's Python: hold its controller cycle lock so no plate
# is being made while services stop. Exit 3 if a cycle is still running at the
# deadline. If the old code cannot read the config (usually because it was
# edited for the new version), the lock cannot be found: say so and exit 0
# without waiting, so an upgrade is never blocked by the version it replaces.
# A broken old runtime that cannot even import exits 1 and refuses.
cycle_lock_program='
import fcntl, os, sys, time
from pathlib import Path
from inky_bird_frame.config import load_config
config_path, ready, timeout = Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3])
try:
    try:
        config = load_config(config_path, load_secrets=False)
    except TypeError:
        config = load_config(config_path)
    state_dir = config.controller.state_dir
except Exception:
    print(
        "The installed version cannot read this configuration, so the upgrade "
        "cannot check for a plate being made.",
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

work_dir=$(mktemp -d)
unit_dir="${work_dir}/units"
unit_backup_dir="${work_dir}/previous-units"
mkdir "${unit_dir}" "${unit_backup_dir}"
new_runtime=""
previous_runtime=""
legacy_runtime=""
venv_before=""
src_before=""
runtime_activated=false
units_replaced=false
controller_was_active=false
controller_restarted=false
installation_complete=false
cycle_lock_pid=""
stopped_units=()
enabled_units=()
active_units=()
not_restarted=()

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

# Each runtime directory holds one source copy and the venv built from it.
# ${app_dir}/.venv and ${app_dir}/src link to the active one, so the units'
# fixed paths switch versions with a rename.
current_runtime() {
  if [ -L "${app_dir}/.venv" ]; then
    (cd -P "${app_dir}/.venv" 2>/dev/null && dirname "$(pwd -P)") || true
  fi
}

replace_link() {
  rm -f "${app_dir}/.runtime-link.next"
  ln -s "$1" "${app_dir}/.runtime-link.next"
  python3 -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' \
    "${app_dir}/.runtime-link.next" "${app_dir}/$2"
}

# How ${app_dir}/<name> looked before this run: a link (and its target), the
# single-directory layout's real directory, or nothing.
describe_path() {
  if [ -L "${app_dir}/$1" ]; then
    printf 'link:%s' "$(readlink "${app_dir}/$1")"
  elif [ -e "${app_dir}/$1" ]; then
    printf 'directory'
  else
    printf 'absent'
  fi
}

restore_path() {
  case "$2" in
    link:*) replace_link "${2#link:}" "$1" ;;
    directory)
      # A directory that was never moved aside is still the live one; keep it.
      if [ -L "${app_dir}/$1" ] || [ ! -e "${app_dir}/$1" ]; then
        replace_link "${legacy_runtime}/$1" "$1"
      fi
      ;;
    *) rm -f "${app_dir}/$1" ;;
  esac
}

# Link order matters if interrupted: a pre-1.0 venv imports ${app_dir}/src, so
# switch .venv first going forward and src first going back.
activate_runtime() {
  local name
  previous_runtime=$(current_runtime)
  # On a Pi that also runs the display, src may link to the display's runtime;
  # rollback restores exactly what was there.
  venv_before=$(describe_path .venv)
  src_before=$(describe_path src)
  runtime_activated=true
  # The single-directory layout becomes the previous runtime on first upgrade.
  for name in .venv src; do
    if [ -e "${app_dir}/${name}" ] && [ ! -L "${app_dir}/${name}" ]; then
      if [ -z "${legacy_runtime}" ]; then
        legacy_runtime=$(mktemp -d "${runtimes_dir}/runtime.XXXXXX")
        previous_runtime=${previous_runtime:-${legacy_runtime}}
      fi
      mv "${app_dir}/${name}" "${legacy_runtime}/${name}"
    fi
  done
  replace_link "${new_runtime}/.venv" .venv
  replace_link "${new_runtime}/src" src
}

deactivate_runtime() {
  restore_path src "${src_before}" && restore_path .venv "${venv_before}"
}

# Only ${app_dir}/.runtimes is pruned; the display keeps its own directory.
prune_runtimes() {
  local runtime
  for runtime in "${runtimes_dir}"/*; do
    if [ ! -d "${runtime}" ] || [ "${runtime}" = "${new_runtime}" ] || \
        [ "${runtime}" = "${previous_runtime}" ]; then
      continue
    fi
    rm -rf "${runtime}"
  done
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
    sleep 1
  done
}

release_cycle_lock() {
  if [ -n "${cycle_lock_pid}" ]; then
    kill "${cycle_lock_pid}" 2>/dev/null || true
    wait "${cycle_lock_pid}" 2>/dev/null || true
    cycle_lock_pid=""
  fi
}

# A service this run started and could not stop still uses the new runtime.
keep_runtime_if_running() {
  case "$1" in
    *.service)
      if systemctl is-active --quiet "$1"; then
        runtime_restored=false
        echo "Could not stop $1; it may still run the new version, kept in ${new_runtime}." >&2
      fi
      ;;
  esac
}

restore_units() {
  local unit name
  local changed=false
  local restored=true
  for unit in "${unit_backup_dir}"/*; do
    if [ ! -f "${unit}" ]; then
      continue
    fi
    name=$(basename "${unit}")
    if ! cmp -s "${unit}" "${unit_root}/${name}"; then
      changed=true
      sudo install -m 0644 "${unit}" "${unit_root}/${name}" || restored=false
    fi
  done
  for unit in "${unit_dir}"/*; do
    name=$(basename "${unit}")
    if [ -f "${unit}" ] && [ ! -e "${unit_backup_dir}/${name}" ] && [ -e "${unit_root}/${name}" ]; then
      changed=true
      if systemctl is-active --quiet "${name}" || systemctl is-enabled --quiet "${name}"; then
        sudo systemctl disable --now "${name}" || restored=false
      fi
      keep_runtime_if_running "${name}"
      sudo rm -f "${unit_root}/${name}" || restored=false
    fi
  done
  if [ "${changed}" = true ]; then
    sudo systemctl daemon-reload || restored=false
  fi
  for name in ${enabled_units[@]+"${enabled_units[@]}"}; do
    if ! systemctl is-enabled --quiet "${name}"; then
      sudo systemctl enable "${name}" || restored=false
    fi
  done
  # Undo starts and enables of existing units that were stopped or disabled
  # before this run; the units this run stopped are started again later.
  for unit in "${unit_backup_dir}"/*; do
    if [ ! -f "${unit}" ]; then
      continue
    fi
    name=$(basename "${unit}")
    if ! contains "${name}" ${active_units[@]+"${active_units[@]}"} \
        ${stopped_units[@]+"${stopped_units[@]}"} && systemctl is-active --quiet "${name}"; then
      sudo systemctl stop "${name}" || restored=false
      keep_runtime_if_running "${name}"
    fi
    if ! contains "${name}" ${enabled_units[@]+"${enabled_units[@]}"} && \
        systemctl is-enabled --quiet "${name}"; then
      sudo systemctl disable "${name}" || restored=false
    fi
  done
  [ "${restored}" = true ]
}

cleanup() {
  status=$?
  trap - EXIT
  release_cycle_lock
  if [ "${installation_complete}" != true ]; then
    runtime_restored=true
    if [ "${units_replaced}" = true ] && ! restore_units; then
      echo "Could not put back every previous service file in ${unit_root}." >&2
    fi
    # A stopped service this run started again runs the new runtime; stop it
    # before switching back so it starts below on the restored one.
    if [ "${runtime_activated}" = true ]; then
      for unit in ${stopped_units[@]+"${stopped_units[@]}"}; do
        case "${unit}" in
          *.service)
            if systemctl is-active --quiet "${unit}" && ! sudo systemctl stop "${unit}"; then
              runtime_restored=false
              echo "Could not stop ${unit}; it still runs the new version, kept in ${new_runtime}." >&2
            fi
            ;;
        esac
      done
    fi
    if [ "${runtime_activated}" = true ] && ! deactivate_runtime; then
      runtime_restored=false
      echo "Could not switch back to the previous runtime; it is kept in ${runtimes_dir}." >&2
    fi
    if [ "${controller_restarted}" = true ] && [ "${controller_was_active}" = true ]; then
      sudo systemctl restart "${service_prefix}-controller.service" || \
        not_restarted+=("${service_prefix}-controller.service")
    fi
    for unit in ${stopped_units[@]+"${stopped_units[@]}"}; do
      sudo systemctl start "${unit}" || not_restarted+=("${unit}")
    done
    if [ -n "${new_runtime}" ] && [ "${runtime_restored}" = true ]; then
      rm -rf "${new_runtime}"
    fi
    if [ "${#not_restarted[@]}" -gt 0 ]; then
      echo "The upgrade failed and these services are stopped: ${not_restarted[*]}." >&2
      echo "Start them with: sudo systemctl start ${not_restarted[*]}" >&2
    elif [ "${runtime_restored}" = true ] && \
        { [ "${#stopped_units[@]}" -gt 0 ] || [ "${controller_restarted}" = true ]; }; then
      echo "The upgrade failed; the previous version is running again." >&2
    fi
  elif [ "${status}" -ne 0 ]; then
    echo "The new version is installed and running, but a final step failed; see the error above." >&2
  fi
  rm -rf "${work_dir}"
  exit "${status}"
}
trap cleanup EXIT

chmod 600 "${config_path}"
mkdir -p "${app_dir}/catalog" "${app_dir}/deploy" "${runtimes_dir}" "${support_dir}"
runtimes_dir=$(cd -P "${runtimes_dir}" && pwd)

# Build and check the new runtime beside the live one; nothing running changes.
new_runtime=$(mktemp -d "${runtimes_dir}/runtime.XXXXXX")
rsync -a --delete "${root}/src/" "${new_runtime}/src/"
mkdir -p "${new_runtime}/deploy" "${new_runtime}/docs"
install -m 0755 "${root}/deploy/install-controller-systemd.sh" "${new_runtime}/deploy/"
for file in pyproject.toml uv.lock README.md LICENSE docs/browser-access.md; do
  install -m 0644 "${root}/${file}" "${new_runtime}/${file}"
done
env -u UV_PROJECT_ENVIRONMENT "${uv_bin}" sync --project "${new_runtime}" \
  --python "${python_version}" --extra controller --locked
# The CLI reports a rejection as a JSON envelope on stdout; show its message.
if ! validation=$("${new_runtime}/.venv/bin/inky-bird" config validate --config "${config_path}"); then
  echo "The new version cannot use this configuration. The running controller was not changed." >&2
  python3 -I -c '
import json, sys
try:
    print(json.loads(sys.argv[1])["error"]["message"])
except (ValueError, KeyError, TypeError):
    print(sys.argv[1])
' "${validation}" >&2
  exit 1
fi

hold_cycle_lock
# Waiting for a plate can outlast cached sudo credentials; ask again before stopping anything.
if ! sudo -v; then
  echo "Administrator access is required to install system services. No changes made." >&2
  exit 1
fi
# App-owned installations use the same dynamic scheduler as Docker. Stop it
# before replacing its runtime; a managed installation removes it below.
if systemctl is-active --quiet "${service_prefix}-scheduler.service"; then
  sudo systemctl stop "${service_prefix}-scheduler.service"
  stopped_units+=("${service_prefix}-scheduler.service")
fi
for name in refresh generate catalog-publish notifications; do
  timer="${service_prefix}-${name}.timer"
  if systemctl is-active --quiet "${timer}"; then
    sudo systemctl stop "${timer}"
    stopped_units+=("${timer}")
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

"${new_runtime}/.venv/bin/python" - \
  "${unit_dir}" "${root}" "${app_dir}" "${config_path}" "${HOME}" "${user}" <<'PY'
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

executable = app_dir / ".venv/bin/inky-bird"
units = controller_systemd_units(
    config,
    executable=executable,
    app_dir=app_dir,
    config_path=config_path,
    home=home,
    user=user,
    service_prefix=os.environ["INKY_BIRD_SERVICE_PREFIX"],
)
for name, content in units.items():
    (unit_dir / name).write_text(content)
PY

if ! "${new_runtime}/.venv/bin/inky-bird" refresh --config "${config_path}"; then
  echo "Could not fetch bird observations yet. The scheduled refresh will try again." >&2
fi

if [ -f "${unit_dir}/${service_prefix}-catalog-publish.timer" ]; then
  "${new_runtime}/.venv/bin/inky-bird" catalog-publish \
    --config "${config_path}" --dry-run
fi

for name in controller scheduler refresh generate catalog-publish notifications; do
  for suffix in service timer; do
    unit="${service_prefix}-${name}.${suffix}"
    if [ -f "${unit_root}/${unit}" ]; then
      cp "${unit_root}/${unit}" "${unit_backup_dir}/${unit}"
    fi
    if systemctl is-enabled --quiet "${unit}"; then
      enabled_units+=("${unit}")
    fi
    if systemctl is-active --quiet "${unit}"; then
      active_units+=("${unit}")
    fi
  done
done
if systemctl is-active --quiet "${service_prefix}-controller.service"; then
  controller_was_active=true
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
controller_restarted=true
sudo systemctl restart "${service_prefix}-controller.service"
if [ -f "${unit_dir}/${service_prefix}-refresh.timer" ]; then
  sudo systemctl restart "${service_prefix}-refresh.timer"
fi
if [ -f "${unit_dir}/${service_prefix}-scheduler.service" ]; then
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
# Keep the previous runtime for rollback; remove anything older.
prune_runtimes
if [ "${root}" != "${app_dir}" ]; then
  mkdir -p "${app_dir}/docs"
  install -m 0755 "${root}/deploy/install-controller-systemd.sh" "${app_dir}/deploy/"
  for file in pyproject.toml uv.lock README.md LICENSE docs/browser-access.md; do
    install -m 0644 "${root}/${file}" "${app_dir}/${file}"
  done
fi
echo "Controller installed from ${root} into ${app_dir}."
echo "Configuration: ${config_path}"
if [ -n "${previous_runtime}" ]; then
  echo "Previous runtime kept for rollback: ${previous_runtime}"
fi
if ! "${app_dir}/.venv/bin/inky-bird" access setup --if-needed --config "${config_path}"; then
  echo "Controller installation completed; owner setup is incomplete. Run access setup in a private terminal. Display and scheduled work continue." >&2
fi
