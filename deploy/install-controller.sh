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
runtimes_dir="${app_dir}/.runtimes"
# setup --yes from the installed CLI runs a runtime's own copy of this script.
# A runtime holds no catalog, so install from the managed directory as before.
if [ "$(cd -P "${root}/.." && pwd)" = "$(cd -P "${runtimes_dir}" 2>/dev/null && pwd || true)" ]; then
  root=${app_dir}
fi

# Run by the old runtime's Python: hold its controller cycle lock so no plate
# is being made while agents stop. Exit 3 if a cycle is still running at the
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

mkdir -p "${app_dir}/catalog" "${app_dir}/deploy" "${runtimes_dir}" "${support_dir}" \
  "${log_dir}" "${agents_dir}"
chmod 700 "${support_dir}" "${log_dir}"
find "${log_dir}" -maxdepth 1 -type f -name '*.log*' -exec chmod 600 {} +
runtimes_dir=$(cd -P "${runtimes_dir}" && pwd)

uid=$(id -u)
work_dir=$(mktemp -d "${support_dir}/install.XXXXXX")
plist_backup_dir="${work_dir}/launchd"
mkdir "${plist_backup_dir}"
new_runtime=""
previous_runtime=""
runtime_activated=false
installation_complete=false
cycle_lock_pid=""
agents_recorded=false
previously_loaded_labels=()
stopped_labels=()
unrestored=()
still_running=()

# Each runtime directory holds one source copy and the venv built from it.
# ${app_dir}/.venv and ${app_dir}/src link to the active one, so the agents'
# fixed paths switch versions with a rename.
current_runtime() {
  if [ -L "${app_dir}/.venv" ]; then
    (cd -P "${app_dir}/.venv" 2>/dev/null && dirname "$(pwd -P)") || true
  fi
}

# Link order matters if interrupted: a pre-1.0 venv imports ${app_dir}/src, so
# switch .venv first going forward and src first going back.
point_runtime() {
  local target=$1
  local name
  shift
  for name in "$@"; do
    # A directory that was never moved aside is still the live one; keep it.
    if [ -e "${app_dir}/${name}" ] && [ ! -L "${app_dir}/${name}" ]; then
      continue
    fi
    rm -f "${app_dir}/.runtime-link.next"
    ln -s "${target}/${name}" "${app_dir}/.runtime-link.next"
    python3 -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' \
      "${app_dir}/.runtime-link.next" "${app_dir}/${name}"
  done
}

activate_runtime() {
  local legacy=""
  local name
  previous_runtime=$(current_runtime)
  runtime_activated=true
  # The single-directory layout becomes the previous runtime on first upgrade.
  for name in .venv src; do
    if [ -e "${app_dir}/${name}" ] && [ ! -L "${app_dir}/${name}" ]; then
      if [ -z "${legacy}" ]; then
        legacy=$(mktemp -d "${runtimes_dir}/runtime.XXXXXX")
        previous_runtime=${previous_runtime:-${legacy}}
      fi
      mv "${app_dir}/${name}" "${legacy}/${name}"
    fi
  done
  point_runtime "${new_runtime}" .venv src
}

deactivate_runtime() {
  if [ -n "${previous_runtime}" ]; then
    point_runtime "${previous_runtime}" src .venv
  else
    rm -f "${app_dir}/.venv" "${app_dir}/src"
  fi
}

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
  # An agent this run did not stop is still running its old definition.
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
    runtime_restored=true
    if [ "${runtime_activated}" = true ] && ! deactivate_runtime; then
      runtime_restored=false
      echo "Could not switch back to the previous runtime; it is kept in ${runtimes_dir}." >&2
    fi
    if [ "${agents_recorded}" = true ]; then
      for label in serve refresh generate catalog-publish notifications scheduler; do
        restore_agent "${label}"
      done
    fi
    # An agent this run started and could not stop still uses the new runtime.
    if [ -n "${new_runtime}" ] && [ "${runtime_restored}" = true ] && \
        [ "${#still_running[@]}" -eq 0 ]; then
      rm -rf "${new_runtime}"
    fi
    if [ "${#still_running[@]}" -gt 0 ]; then
      echo "The upgrade failed and these agents could not be stopped: ${still_running[*]}." >&2
      if [ "${runtime_restored}" = true ] && [ -n "${previous_runtime}" ]; then
        echo "A copy already running uses the new version, kept in ${new_runtime}; when it starts again it uses the previous version." >&2
      else
        echo "A copy already running uses the new version, kept in ${new_runtime}." >&2
      fi
      echo "Stop each with: launchctl bootout gui/${uid}/<agent>" >&2
    fi
    if [ "${#unrestored[@]}" -gt 0 ]; then
      echo "The upgrade failed and these agents are not running: ${unrestored[*]}." >&2
      echo "Start each with: launchctl bootstrap gui/${uid} ${agents_dir}/<agent>.plist" >&2
    elif [ "${#stopped_labels[@]}" -gt 0 ] && [ "${#still_running[@]}" -eq 0 ]; then
      echo "The upgrade failed; the previous version is running again." >&2
    fi
  elif [ "${status}" -ne 0 ]; then
    echo "The new version is installed and running, but a final step failed; see the error above." >&2
  fi
  rm -rf "${work_dir}"
  exit "${status}"
}
trap cleanup EXIT

# Build and check the new runtime beside the live one; nothing running changes.
new_runtime=$(mktemp -d "${runtimes_dir}/runtime.XXXXXX")
rsync -a --delete "${root}/src/" "${new_runtime}/src/"
mkdir -p "${new_runtime}/deploy" "${new_runtime}/docs"
install -m 0755 "${root}/deploy/install-controller.sh" "${new_runtime}/deploy/"
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
  "${new_runtime}/.venv/bin/inky-bird" catalog-publish --config "${config_path}" --dry-run
fi

for label in serve refresh generate catalog-publish notifications scheduler; do
  if launchctl print "gui/${uid}/com.${service_prefix}.${label}" >/dev/null 2>&1; then
    launchctl bootout "gui/${uid}/com.${service_prefix}.${label}"
    stopped_labels+=("${label}")
  fi
done
release_cycle_lock
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

installation_complete=true
# Keep the previous runtime for rollback; remove anything older.
prune_runtimes
if [ "${root}" != "${app_dir}" ]; then
  mkdir -p "${app_dir}/docs"
  install -m 0755 "${root}/deploy/install-controller.sh" "${app_dir}/deploy/"
  for file in pyproject.toml uv.lock README.md LICENSE docs/browser-access.md; do
    install -m 0644 "${root}/${file}" "${app_dir}/${file}"
  done
fi
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
  echo "Previous runtime kept for rollback: ${previous_runtime}"
fi
if ! "${app_dir}/.venv/bin/inky-bird" access setup --if-needed --config "${config_path}"; then
  echo "Controller installation completed; owner setup is incomplete. Run access setup in a private terminal. Display and scheduled work continue." >&2
fi
