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
user=$(id -un)

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

work_dir=$(mktemp -d)
unit_dir="${work_dir}/units"
mkdir "${unit_dir}"
installation_complete=false
cycle_lock_pid=""
stopped_units=()
not_restarted=()

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

# A unit this run removed on purpose is not one to start again on failure.
forget_stopped() {
  local kept=()
  local unit
  for unit in ${stopped_units[@]+"${stopped_units[@]}"}; do
    if [ "${unit}" != "$1" ]; then
      kept+=("${unit}")
    fi
  done
  stopped_units=(${kept[@]+"${kept[@]}"})
}

cleanup() {
  status=$?
  trap - EXIT
  release_cycle_lock
  if [ "${installation_complete}" != true ]; then
    for unit in ${stopped_units[@]+"${stopped_units[@]}"}; do
      if ! sudo systemctl start "${unit}" && ! systemctl is-active --quiet "${unit}"; then
        not_restarted+=("${unit}")
      fi
    done
    if [ "${#not_restarted[@]}" -gt 0 ]; then
      echo "The upgrade failed and these services are stopped: ${not_restarted[*]}." >&2
      echo "Start them with: sudo systemctl start ${not_restarted[*]}" >&2
    elif [ "${#stopped_units[@]}" -gt 0 ]; then
      echo "The upgrade failed; the services it stopped were started again." >&2
    fi
  fi
  rm -rf "${work_dir}"
  exit "${status}"
}
trap cleanup EXIT

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
# The wait above can also outlast cached credentials; the steps below need them.
if ! sudo -v; then
  echo "Administrator access is required to install system services." >&2
  exit 1
fi

chmod 600 "${config_path}"
mkdir -p "${app_dir}/src" "${app_dir}/catalog" "${app_dir}/deploy" "${support_dir}"
if [ "${root}" != "${app_dir}" ]; then
  mkdir -p "${app_dir}/docs"
  rsync -a --delete "${root}/src/" "${app_dir}/src/"
  install -m 0755 "${root}/deploy/install-controller-systemd.sh" "${app_dir}/deploy/"
  for file in pyproject.toml uv.lock README.md LICENSE docs/browser-access.md; do
    install -m 0644 "${root}/${file}" "${app_dir}/${file}"
  done
fi

# A stray UV_PROJECT_ENVIRONMENT would build the venv somewhere the units do not run.
env -u UV_PROJECT_ENVIRONMENT "${uv_bin}" sync --project "${app_dir}" \
  --python "${python_version}" --extra controller --locked

"${app_dir}/.venv/bin/python" - \
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

# Only an unreachable bird source may wait for the scheduled refresh; any other
# failure means the new version does not work here.
refresh_status=0
refresh_output=$("${app_dir}/.venv/bin/inky-bird" refresh --config "${config_path}") || \
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
  "${app_dir}/.venv/bin/inky-bird" catalog-publish \
    --config "${config_path}" --dry-run
fi

for unit in "${unit_dir}"/*; do
  sudo install -m 0644 "${unit}" "/etc/systemd/system/$(basename "${unit}")"
done
for optional in refresh generate catalog-publish notifications; do
  if [ ! -f "${unit_dir}/${service_prefix}-${optional}.timer" ]; then
    if [ -e "/etc/systemd/system/${service_prefix}-${optional}.timer" ]; then
      sudo systemctl disable --now "${service_prefix}-${optional}.timer"
    fi
    forget_stopped "${service_prefix}-${optional}.timer"
    sudo rm -f \
      "/etc/systemd/system/${service_prefix}-${optional}.service" \
      "/etc/systemd/system/${service_prefix}-${optional}.timer"
  fi
done
if [ ! -f "${unit_dir}/${service_prefix}-scheduler.service" ]; then
  if [ -e "/etc/systemd/system/${service_prefix}-scheduler.service" ]; then
    sudo systemctl disable --now "${service_prefix}-scheduler.service"
  fi
  forget_stopped "${service_prefix}-scheduler.service"
  sudo rm -f "/etc/systemd/system/${service_prefix}-scheduler.service"
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
echo "Controller installed from ${root} into ${app_dir}."
echo "Configuration: ${config_path}"
if ! "${app_dir}/.venv/bin/inky-bird" access setup --if-needed --config "${config_path}"; then
  echo "Controller installation completed; owner setup is incomplete. Run access setup in a private terminal. Display and scheduled work continue." >&2
fi
