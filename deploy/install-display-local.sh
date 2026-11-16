#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != "Linux" ] || ! command -v systemctl >/dev/null 2>&1; then
  echo "Display installer requires Linux with systemd." >&2
  exit 1
fi

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
venv=${INKY_BIRD_DISPLAY_VENV:-"${HOME}/.virtualenvs/pimoroni"}
run_initial_display=${INKY_BIRD_RUN_INITIAL_DISPLAY:-false}
noninteractive_sudo=${INKY_BIRD_NONINTERACTIVE_SUDO:-false}
unit_root="/etc/systemd/system"

# Remove one distribution's files, except those the other one also owns.
remove_distribution_program='
import sys
from importlib.metadata import PackageNotFoundError, distribution
try:
    removed = distribution(sys.argv[1])
except PackageNotFoundError:
    sys.exit(0)
kept = distribution(sys.argv[2])
owned = {kept.locate_file(path).resolve() for path in kept.files or ()}
directories = set()
for path in removed.files or ():
    target = removed.locate_file(path)
    if target.resolve() in owned or not (target.is_file() or target.is_symlink()):
        continue
    target.unlink()
    directories.add(target.parent)
for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
    try:
        directory.rmdir()
    except OSError:
        pass
'
validate_program='
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from inky_bird_frame.config import load_config
load_config(Path(sys.argv[1]))
'
# The project an editable install was made from (a regular install has none).
installed_source_program='
import importlib.util
from pathlib import Path
spec = importlib.util.find_spec("inky_bird_frame")
if spec is not None and spec.origin:
    project = Path(spec.origin).parents[2]
    if (project / "pyproject.toml").is_file():
        print(project)
'

# The managed directory's copy of this script reinstalls the active runtime; a
# source checkout used as that directory installs what it holds.
if [ -d "${root}/.display-runtimes" ] && [ -f "${root}/deploy/.managed-copy" ] && \
    [ -x "${venv}/bin/python" ]; then
  active=$("${venv}/bin/python" -I -c "${installed_source_program}" 2>/dev/null || true)
  if [ -n "${active}" ] && [ "$(cd -P "${active}/.." 2>/dev/null && pwd || true)" = \
      "$(cd -P "${root}/.display-runtimes" && pwd)" ]; then
    root=${active}
  fi
fi
native_names=$(python3 "${root}/src/inky_bird_frame/native_names.py")
service_prefix="" app_dir="" support_dir="" config_path=""
eval "${native_names}"
export INKY_BIRD_SERVICE_PREFIX="${service_prefix}"
# The controller keeps its runtimes in ${app_dir}/.runtimes; one Pi can run
# both roles, so each role keeps and prunes only its own directory.
runtimes_dir="${app_dir}/.display-runtimes"
# setup --yes from the installed CLI runs the active runtime's own copy of this
# script. A runtime holds no catalog; that stays in the managed directory.
catalog_root=${root}
if [ "$(cd -P "${root}/.." && pwd)" = "$(cd -P "${runtimes_dir}" 2>/dev/null && pwd || true)" ]; then
  catalog_root=${app_dir}
fi
service="${service_prefix}-display.service"
timer="${service_prefix}-display.timer"

if [ ! -f "${config_path}" ]; then
  echo "Display-node configuration is missing: ${config_path}" >&2
  exit 1
fi
if [ ! -x "${venv}/bin/python" ]; then
  echo "Pimoroni Python environment is missing: ${venv}" >&2
  exit 1
fi
# Distribution ownership changes only while the display role is quiescent.
# setup is not a substitute for an operator's backup/upgrade transaction.
installed=""
if "${venv}/bin/python" -m pip show inky-bird-frame >/dev/null 2>&1; then
  installed=inky-bird-frame
elif "${venv}/bin/python" -m pip show inky-bird >/dev/null 2>&1; then
  installed=inky-bird
fi
if [ -n "${installed}" ]; then
  for suffix in timer service; do
    state=$(systemctl show --property=ActiveState --value "${service_prefix}-display.${suffix}")
    case "${state}" in
      inactive | failed) ;;
      *)
        echo "Stop the existing display timer and let its service finish before updating its runtime. Preserve a recoverable runtime backup first. No changes made." >&2
        exit 1
        ;;
    esac
  done
fi
sudo_command=(sudo)
case "${noninteractive_sudo}" in
  true)
    sudo_command+=(-n)
    if ! "${sudo_command[@]}" -v; then
      echo "Noninteractive administrator access is required before installing the display service. No changes made." >&2
      exit 1
    fi
    ;;
  false)
    if ! sudo -v; then
      echo "Administrator access is required to install the display service." >&2
      exit 1
    fi
    ;;
  *)
    echo "INKY_BIRD_NONINTERACTIVE_SUDO must be true or false." >&2
    exit 1
    ;;
esac


work_dir=""
unit_dir=""
unit_backup_dir=""
created_runtimes_dir=false
new_runtime=""
previous_runtime=""
installed_source=""
enabled_before=()
active_before=()
units_recorded=false
package_installed=false
old_package_removed=false
units_replaced=false
units_stuck=false
installation_complete=false
new_runtime_in_use=false
new_runtime_shadowed=false

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
  install -m 0755 "${root}/deploy/install-display-local.sh" "${target}/deploy/"
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

# Take the new distribution back out and leave the previous one as it was.
restore_package() {
  case "${installed}" in
    "")
      "${venv}/bin/python" -m pip uninstall --disable-pip-version-check -y inky-bird
      ;;
    inky-bird-frame)
      # While the renamed distribution is still what Python imports, removing
      # the new one leaves the previous version exactly as it was.
      if [ "${old_package_removed}" = true ] && [ "$("${venv}/bin/python" -I -c \
          "${installed_source_program}" 2>/dev/null || true)" = "${new_runtime}" ]; then
        return 1
      fi
      "${venv}/bin/python" -I -c "${remove_distribution_program}" inky-bird inky-bird-frame
      ;;
    inky-bird)
      [ -n "${installed_source}" ] && \
        "${venv}/bin/python" -m pip install --disable-pip-version-check -e "${installed_source}[inky]"
      ;;
  esac
}

# Return the display units to the files and state recorded before the run.
restore_units() {
  local unit
  local changed=false
  if [ "${units_replaced}" = true ]; then
    for unit in "${service}" "${timer}"; do
      if [ -f "${unit_backup_dir}/${unit}" ]; then
        if ! cmp -s "${unit_backup_dir}/${unit}" "${unit_root}/${unit}"; then
          changed=true
          "${sudo_command[@]}" install -m 0644 "${unit_backup_dir}/${unit}" "${unit_root}/${unit}" || true
        fi
      elif [ -e "${unit_root}/${unit}" ]; then
        changed=true
        if { systemctl is-active --quiet "${unit}" || systemctl is-enabled --quiet "${unit}"; } && \
            ! "${sudo_command[@]}" systemctl disable --now "${unit}"; then
          # Keep its file so a rerun or the uninstaller can still find and stop it.
          units_stuck=true
          continue
        fi
        "${sudo_command[@]}" rm -f "${unit_root}/${unit}" || true
      fi
    done
    if [ "${changed}" = true ]; then
      "${sudo_command[@]}" systemctl daemon-reload || true
    fi
  fi
  if [ "${units_recorded}" != true ]; then
    return 0
  fi
  for unit in "${service}" "${timer}"; do
    if [ ! -e "${unit_root}/${unit}" ]; then
      continue
    fi
    if contains "${unit}" ${enabled_before[@]+"${enabled_before[@]}"}; then
      systemctl is-enabled --quiet "${unit}" || "${sudo_command[@]}" systemctl enable "${unit}" || true
    elif systemctl is-enabled --quiet "${unit}"; then
      "${sudo_command[@]}" systemctl disable "${unit}" || true
    fi
    if contains "${unit}" ${active_before[@]+"${active_before[@]}"}; then
      systemctl is-active --quiet "${unit}" || "${sudo_command[@]}" systemctl start "${unit}" || true
    elif systemctl is-active --quiet "${unit}"; then
      "${sudo_command[@]}" systemctl stop "${unit}" || true
    fi
  done
}

# Name every unit that is not back as it was, from what systemd reports now.
report_units() {
  local unit
  local differ=()
  for unit in "${service}" "${timer}"; do
    if [ -e "${unit_root}/${unit}" ]; then
      if [ "${units_replaced}" = true ] && { [ ! -f "${unit_backup_dir}/${unit}" ] || \
          ! cmp -s "${unit_backup_dir}/${unit}" "${unit_root}/${unit}"; }; then
        differ+=("${unit}")
        continue
      fi
    elif [ -f "${unit_backup_dir}/${unit}" ]; then
      differ+=("${unit}")
      continue
    fi
    if [ "${units_recorded}" != true ]; then
      continue
    fi
    # Enabled and running are restored separately; either can be left wrong.
    if contains "${unit}" ${enabled_before[@]+"${enabled_before[@]}"}; then
      systemctl is-enabled --quiet "${unit}" || differ+=("${unit}")
    elif systemctl is-enabled --quiet "${unit}"; then
      differ+=("${unit}")
    fi
    if contains "${unit}" ${differ[@]+"${differ[@]}"}; then
      continue
    fi
    if contains "${unit}" ${active_before[@]+"${active_before[@]}"}; then
      systemctl is-active --quiet "${unit}" || differ+=("${unit}")
    elif systemctl is-active --quiet "${unit}"; then
      differ+=("${unit}")
    fi
  done
  if [ "${#differ[@]}" -gt 0 ]; then
    echo "These display services are not as they were before the upgrade: ${differ[*]}." >&2
    echo "Check them with: systemctl status ${differ[*]}" >&2
    return 1
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
  if [ "${installation_complete}" != true ]; then
    restore_units
    # A fresh install's unit that could not be stopped or disabled keeps the
    # new package to run, not a missing one.
    if [ "${package_installed}" = true ] && [ -z "${installed}" ] && [ "${units_stuck}" = true ]; then
      new_runtime_in_use=true
    elif [ "${package_installed}" = true ] && ! restore_package; then
      new_runtime_in_use=true
      # Python may still import the renamed distribution before the new one;
      # if that cannot be checked, the new one is taken to be in use.
      if [ "${installed}" = inky-bird-frame ] && imported=$("${venv}/bin/python" -I -c \
          "${installed_source_program}" 2>/dev/null) && [ "${imported}" != "${new_runtime}" ]; then
        new_runtime_shadowed=true
      fi
    fi
    units_restored=true
    report_units || units_restored=false
    if [ -n "${new_runtime}" ] && [ "${new_runtime_in_use}" != true ] && \
        ! rm -rf "${new_runtime}"; then
      echo "Could not remove what this upgrade added; nothing uses it, so remove it by hand: ${new_runtime}" >&2
    fi
    if [ "${created_runtimes_dir}" = true ]; then
      rmdir "${runtimes_dir}" 2>/dev/null || true
    fi
    if [ "${new_runtime_shadowed}" = true ]; then
      echo "The upgrade failed; the display still runs the previous version, and the new version could not be taken out. Rerun the installer to finish; the new version is kept in ${new_runtime}." >&2
    elif [ "${new_runtime_in_use}" = true ]; then
      echo "The upgrade failed after the new version was installed; the display uses the new version, kept in ${new_runtime}." >&2
    elif [ -n "${installed}" ] && [ "${package_installed}" = true ] && [ "${units_restored}" = true ]; then
      echo "The upgrade failed; the previous version is unchanged." >&2
    fi
    if [ "${status}" -eq 0 ]; then
      status=1
    fi
  elif [ "${status}" -ne 0 ]; then
    echo "The new version is installed, but a final step failed; see the error above." >&2
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
if [ -n "${installed}" ]; then
  installed_source=$("${venv}/bin/python" -I -c "${installed_source_program}")
fi

# Stage and check the new runtime beside the live one; nothing running changes.
make_temp_dir new_runtime "${runtimes_dir}/runtime.XXXXXX"
stage_runtime_source "${new_runtime}"
if ! "${venv}/bin/python" -I -c "${validate_program}" "${config_path}" "${new_runtime}/src"; then
  echo "The new version cannot use this configuration. No changes made." >&2
  exit 1
fi

for unit in "${service}" "${timer}"; do
  if [ -f "${unit_root}/${unit}" ]; then
    cp "${unit_root}/${unit}" "${unit_backup_dir}/${unit}"
  fi
  if systemctl is-enabled --quiet "${unit}"; then
    enabled_before+=("${unit}")
  fi
  if systemctl is-active --quiet "${unit}"; then
    active_before+=("${unit}")
  fi
done
units_recorded=true

# A failed install leaves the old distribution in place: pip restores a
# same-named one itself, and the renamed one is not touched until later.
package_installed=true
"${venv}/bin/python" -m pip install --disable-pip-version-check -e "${new_runtime}[inky]"

"${venv}/bin/python" -I - \
  "${unit_dir}" "${app_dir}" "${config_path}" "${venv}" "$(id -un)" "${new_runtime}/src" <<'PY'
import os
import sys
from pathlib import Path

# Until the renamed distribution is removed below, its regular install can
# shadow the staged editable one; render the units with the staged code.
sys.path.insert(0, sys.argv[6])
from inky_bird_frame.config import load_config
from inky_bird_frame.installation import display_systemd_units


unit_dir, app_dir, config_path, venv = map(Path, sys.argv[1:5])
user = sys.argv[5]
service_prefix = os.environ["INKY_BIRD_SERVICE_PREFIX"]
# The command named like the services: 0.9 installs had only inky-bird-frame,
# so a service whose definition a failed upgrade could not put back still starts.
executable = venv / "bin" / service_prefix
config = load_config(config_path)
units = display_systemd_units(
    config,
    executable=executable,
    app_dir=app_dir,
    config_path=config_path,
    user=user,
    service_prefix=service_prefix,
)
for name, content in units.items():
    (unit_dir / name).write_text(content)
PY

if [ "${catalog_root}" != "${app_dir}" ]; then
  rsync -a "${catalog_root}/catalog/" "${app_dir}/catalog/"
fi

units_replaced=true
"${sudo_command[@]}" install -m 0644 "${unit_dir}/${service}" "${unit_root}/${service}"
"${sudo_command[@]}" install -m 0644 "${unit_dir}/${timer}" "${unit_root}/${timer}"
"${sudo_command[@]}" systemctl daemon-reload
"${sudo_command[@]}" systemctl enable "${service_prefix}-display.timer"
"${sudo_command[@]}" systemctl restart "${service_prefix}-display.timer"
if [ "${run_initial_display}" = true ]; then
  "${sudo_command[@]}" systemctl start "${service_prefix}-display.service"
fi
systemctl is-enabled --quiet "${service_prefix}-display.timer"
systemctl is-active --quiet "${service_prefix}-display.timer"

# The renamed distribution's regular install shadows the new editable one, so
# the previous version keeps running until this point. Both list some of the
# same files, such as the inky-bird command; remove only the files the new one
# does not also own.
if [ "${installed}" = inky-bird-frame ]; then
  old_package_removed=true
  "${venv}/bin/python" -I -c "${remove_distribution_program}" inky-bird-frame inky-bird
fi
installation_complete=true
# Keep the previous runtime for rollback; nothing runs from anything older.
if [ -n "${installed_source}" ] && \
    [ "$(cd -P "${installed_source}/.." 2>/dev/null && pwd || true)" = "${runtimes_dir}" ]; then
  previous_runtime=${installed_source}
fi
for runtime in "${runtimes_dir}"/*; do
  if [ -d "${runtime}" ] && [ "${runtime}" != "${new_runtime}" ] && \
      [ "${runtime}" != "${previous_runtime}" ] && ! rm -rf "${runtime}"; then
    echo "Could not remove an older runtime; remove it by hand: ${runtime}" >&2
  fi
done
# The managed directory's copy of this script reinstalls the active runtime;
# the mark tells it apart from a source checkout used as that directory.
if [ ! "${root}/deploy" -ef "${app_dir}/deploy" ]; then
  install -m 0755 "${root}/deploy/install-display-local.sh" "${app_dir}/deploy/"
  : >"${app_dir}/deploy/.managed-copy"
fi
echo "Display node installed from ${root} into ${app_dir}."
echo "Configuration: ${config_path}"
if [ -n "${previous_runtime}" ]; then
  echo "Previous version kept for rollback: ${previous_runtime}"
fi
