#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
  echo "Controller installer currently supports macOS." >&2
  exit 1
fi

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
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
log_dir="${support_dir}/logs"
agents_dir="${HOME}/Library/LaunchAgents"
serve_plist="${agents_dir}/com.${service_prefix}.serve.plist"
refresh_plist="${agents_dir}/com.${service_prefix}.refresh.plist"
generation_plist="${agents_dir}/com.${service_prefix}.generate.plist"
catalog_publish_plist="${agents_dir}/com.${service_prefix}.catalog-publish.plist"
notifications_plist="${agents_dir}/com.${service_prefix}.notifications.plist"
scheduler_plist="${agents_dir}/com.${service_prefix}.scheduler.plist"
legacy_cycle_plist="${agents_dir}/com.${service_prefix}.controller-cycle.plist"

# Run by the installed version's Python: hold its controller cycle lock so no
# plate is being made while agents stop. Exit 3 if a cycle is still running at
# the deadline. If the installed code cannot be loaded or cannot read the
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

restore_catalog_publisher_schedule() {
  local uid=$1
  local recovery_dir
  local recovery_plist
  recovery_dir=$(mktemp -d "${support_dir}/catalog-publish-recovery.XXXXXX")
  recovery_plist="${recovery_dir}/catalog-publish.plist"
  if ! cp "${catalog_publish_plist}" "${recovery_plist}"; then
    rmdir "${recovery_dir}"
    return 1
  fi
  if ! /usr/bin/plutil -replace RunAtLoad -bool false "${recovery_plist}"; then
    rm -f "${recovery_plist}"
    rmdir "${recovery_dir}"
    return 1
  fi
  if ! launchctl bootstrap "gui/${uid}" "${recovery_plist}"; then
    rm -f "${recovery_plist}"
    rmdir "${recovery_dir}"
    return 1
  fi
  rm -f "${recovery_plist}"
  rmdir "${recovery_dir}"
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

mkdir -p "${app_dir}/catalog" "${app_dir}/deploy" "${support_dir}" "${log_dir}" "${agents_dir}"
chmod 700 "${support_dir}" "${log_dir}"
find "${log_dir}" -maxdepth 1 -type f -name '*.log*' -exec chmod 600 {} +
uid=$(id -u)
work_dir=$(mktemp -d "${support_dir}/install.XXXXXX")
plist_backup_dir="${work_dir}/launchd"
mkdir "${plist_backup_dir}"
installation_complete=false
cycle_lock_pid=""
agents_recorded=false
previously_loaded_labels=()
stopped_labels=()
unrestored=()
still_running=()

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

# Put one agent back as it was: its saved plist, and running again only if
# this run stopped it. An agent this run never stopped keeps running.
restore_agent() {
  local label=$1
  local agent="com.${service_prefix}.${label}"
  local plist="${agents_dir}/com.${service_prefix}.${label}.plist"
  local backup="${plist_backup_dir}/com.${service_prefix}.${label}.plist"
  local stopped=false
  local loaded_before=false
  if contains "${label}" ${stopped_labels[@]+"${stopped_labels[@]}"}; then
    stopped=true
  fi
  if contains "${label}" ${previously_loaded_labels[@]+"${previously_loaded_labels[@]}"}; then
    loaded_before=true
  fi
  # Unload only what this run loaded: a new agent or a replacement.
  if { [ "${stopped}" = true ] || [ "${loaded_before}" = false ]; } && \
      launchctl print "gui/${uid}/${agent}" >/dev/null 2>&1; then
    if ! launchctl bootout "gui/${uid}/${agent}"; then
      still_running+=("${agent}")
      return 0
    fi
  fi
  if [ -f "${backup}" ]; then
    if ! cp "${backup}" "${plist}"; then
      unrestored+=("${agent}")
      return 0
    fi
  else
    rm -f "${plist}"
  fi
  if [ "${stopped}" != true ]; then
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
  release_cycle_lock
  if [ "${installation_complete}" != true ]; then
    if [ "${agents_recorded}" = true ]; then
      for label in serve refresh generate catalog-publish notifications scheduler; do
        restore_agent "${label}"
      done
    fi
    if [ "${#still_running[@]}" -gt 0 ]; then
      echo "The upgrade failed and these agents could not be stopped: ${still_running[*]}." >&2
      echo "Stop each with: launchctl bootout gui/${uid}/<agent>" >&2
    fi
    if [ "${#unrestored[@]}" -gt 0 ]; then
      echo "The upgrade failed and these agents are not running: ${unrestored[*]}." >&2
      echo "Start each with: launchctl bootstrap gui/${uid} ${agents_dir}/<agent>.plist" >&2
    elif [ "${#stopped_labels[@]}" -gt 0 ] && [ "${#still_running[@]}" -eq 0 ]; then
      echo "The upgrade failed; the agents it stopped were started again." >&2
    fi
  elif [ "${status}" -ne 0 ]; then
    echo "The new version is installed and running, but a final step failed; see the error above." >&2
  fi
  rm -rf "${work_dir}"
  exit "${status}"
}
trap cleanup EXIT

hold_cycle_lock

if [ "${root}" != "${app_dir}" ]; then
  mkdir -p "${app_dir}/docs"
  rsync -a --delete "${root}/src/" "${app_dir}/src/"
  install -m 0755 "${root}/deploy/install-controller.sh" "${app_dir}/deploy/"
  for file in pyproject.toml uv.lock README.md LICENSE docs/browser-access.md; do
    install -m 0644 "${root}/${file}" "${app_dir}/${file}"
  done
fi

# A stray UV_PROJECT_ENVIRONMENT would build the venv somewhere the agents do not run.
env -u UV_PROJECT_ENVIRONMENT "${uv_bin}" sync --project "${app_dir}" \
  --python "${python_version}" --extra controller --locked

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

"${app_dir}/.venv/bin/python" - \
  "${serve_plist}" "${refresh_plist}" "${generation_plist}" "${catalog_publish_plist}" \
  "${notifications_plist}" "${scheduler_plist}" \
  "${root}" "${app_dir}" "${config_path}" "${log_dir}" <<'PY'
import plistlib
import os
import sys
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
) = map(Path, sys.argv[1:])
executable = app_dir / ".venv/bin/inky-bird"
service_prefix = os.environ["INKY_BIRD_SERVICE_PREFIX"]
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
    launchctl bootout "gui/${uid}/com.${service_prefix}.catalog-publish"
    stopped_labels+=("catalog-publish")
  fi
  # A failure here puts the publisher back, without RunAtLoad, on exit.
  "${app_dir}/.venv/bin/inky-bird" catalog-publish --config "${config_path}" --dry-run
fi

for label in serve refresh generate catalog-publish notifications scheduler; do
  if launchctl print "gui/${uid}/com.${service_prefix}.${label}" >/dev/null 2>&1; then
    launchctl bootout "gui/${uid}/com.${service_prefix}.${label}"
    stopped_labels+=("${label}")
  fi
done
release_cycle_lock
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

installation_complete=true
# Remove the legacy single-agent layout only after the new agents verified,
# so a failed upgrade leaves the previous controller runnable.
if launchctl print "gui/${uid}/com.${service_prefix}.controller-cycle" >/dev/null 2>&1; then
  launchctl bootout "gui/${uid}/com.${service_prefix}.controller-cycle"
fi
rm -f "${legacy_cycle_plist}"
echo "Controller installed from ${root} into ${app_dir}."
echo "Configuration: ${config_path}"
echo "Logs: ${log_dir}"
if ! "${app_dir}/.venv/bin/inky-bird" access setup --if-needed --config "${config_path}"; then
  echo "Controller installation completed; owner setup is incomplete. Run access setup in a private terminal. Display and scheduled work continue." >&2
fi
