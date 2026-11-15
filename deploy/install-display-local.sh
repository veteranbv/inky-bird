#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != "Linux" ] || ! command -v systemctl >/dev/null 2>&1; then
  echo "Display installer requires Linux with systemd." >&2
  exit 1
fi

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
native_names=$(python3 "${root}/src/inky_bird_frame/native_names.py")
service_prefix="" app_dir="" support_dir="" config_path=""
eval "${native_names}"
export INKY_BIRD_SERVICE_PREFIX="${service_prefix}"
venv=${INKY_BIRD_DISPLAY_VENV:-"${HOME}/.virtualenvs/pimoroni"}
run_initial_display=${INKY_BIRD_RUN_INITIAL_DISPLAY:-false}
noninteractive_sudo=${INKY_BIRD_NONINTERACTIVE_SUDO:-false}
unit_root="/etc/systemd/system"
# The controller keeps its runtimes in ${app_dir}/.runtimes; one Pi can run both
# roles, so each role prunes only its own directory.
runtimes_dir="${app_dir}/.display-runtimes"
# setup --yes from the installed CLI runs a runtime's own copy of this script.
# A runtime holds no catalog, so install from the managed directory as before.
if [ "$(cd -P "${root}/.." && pwd)" = "$(cd -P "${runtimes_dir}" 2>/dev/null && pwd || true)" ]; then
  root=${app_dir}
fi
timer="${service_prefix}-display.timer"

# Remove one distribution's files, except those the other one also owns.
remove_distribution_program='
import sys
from importlib.metadata import distribution
removed, kept = distribution(sys.argv[1]), distribution(sys.argv[2])
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

if [ ! -f "${config_path}" ]; then
  echo "Display-node configuration is missing: ${config_path}" >&2
  exit 1
fi
if [ ! -x "${venv}/bin/python" ]; then
  echo "Pimoroni Python environment is missing: ${venv}" >&2
  exit 1
fi
installed=""
if "${venv}/bin/python" -m pip show inky-bird-frame >/dev/null 2>&1; then
  installed=inky-bird-frame
elif "${venv}/bin/python" -m pip show inky-bird >/dev/null 2>&1; then
  installed=inky-bird
fi
# Distribution ownership changes only while the display role is quiescent.
# setup is not a substitute for an operator's backup/upgrade transaction.
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

work_dir=$(mktemp -d)
unit_dir="${work_dir}/units"
unit_backup_dir="${work_dir}/previous-units"
mkdir "${unit_dir}" "${unit_backup_dir}"
new_runtime=""
previous_runtime=""
installed_source=""
source_link=""
moved_runtime=""
timer_was_enabled=false
package_installed=false
old_package_removed=false
runtime_linked=false
units_replaced=false
installation_complete=false

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
      "${sudo_command[@]}" install -m 0644 "${unit}" "${unit_root}/${name}" || restored=false
    fi
  done
  for name in "${service_prefix}-display.service" "${timer}"; do
    if [ ! -e "${unit_backup_dir}/${name}" ] && [ -e "${unit_root}/${name}" ]; then
      changed=true
      if systemctl is-active --quiet "${name}" || systemctl is-enabled --quiet "${name}"; then
        if ! "${sudo_command[@]}" systemctl disable --now "${name}"; then
          restored=false
          # Keep its file so a rerun or the uninstaller can still find and stop it.
          continue
        fi
      fi
      "${sudo_command[@]}" rm -f "${unit_root}/${name}" || restored=false
    fi
  done
  if [ "${changed}" = true ]; then
    "${sudo_command[@]}" systemctl daemon-reload || restored=false
  fi
  [ "${restored}" = true ]
}

link_source() {
  rm -f "${app_dir}/.runtime-link.next"
  ln -s "$1" "${app_dir}/.runtime-link.next"
  python3 -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' \
    "${app_dir}/.runtime-link.next" "${app_dir}/src"
}

# Put ${app_dir}/src back exactly as it was; on a Pi that also runs the
# controller it may have linked to the controller's runtime.
restore_source() {
  if [ -n "${source_link}" ]; then
    link_source "${source_link}"
  elif [ -n "${moved_runtime}" ]; then
    link_source "${moved_runtime}/src"
  else
    rm -f "${app_dir}/src"
  fi
}

same_directory() {
  [ "$(cd -P "$1" 2>/dev/null && pwd -P)" = "$(cd -P "$2" 2>/dev/null && pwd -P)" ]
}

# Reinstall the same-named previous version from the source it was built from.
reinstall_previous() {
  local source=${installed_source}
  if [ "${runtime_linked}" = true ]; then
    restore_source || return 1
    if [ -n "${moved_runtime}" ] && [ -n "${source}" ] && same_directory "${source}" "${app_dir}"; then
      source=${moved_runtime}
    fi
  fi
  if [ -z "${source}" ]; then
    return 1
  fi
  "${venv}/bin/python" -m pip install --disable-pip-version-check -e "${source}[inky]"
}

cleanup() {
  status=$?
  trap - EXIT
  if [ "${installation_complete}" != true ]; then
    previous_kept=true
    if [ "${units_replaced}" = true ] && ! restore_units; then
      echo "Could not put back the previous display service files in ${unit_root}." >&2
    fi
    if [ "${package_installed}" = true ]; then
      previous_kept=false
      # Before the old distribution is removed, taking the new one out again
      # leaves the previous version exactly as it was.
      if [ "${installed}" = inky-bird-frame ]; then
        if [ "${old_package_removed}" != true ] && \
            "${venv}/bin/python" -I -c "${remove_distribution_program}" inky-bird inky-bird-frame; then
          previous_kept=true
        fi
      elif [ "${installed}" = inky-bird ] && reinstall_previous; then
        previous_kept=true
      fi
    fi
    if [ "${previous_kept}" = true ] && [ -n "${new_runtime}" ]; then
      rm -rf "${new_runtime}"
    fi
    # A timer this run restarted keeps its new schedule until it is restarted.
    timer_action=start
    if [ "${units_replaced}" = true ]; then
      timer_action=restart
    fi
    if [ -n "${installed}" ] && [ "${timer_was_enabled}" = true ]; then
      if ! "${sudo_command[@]}" systemctl "${timer_action}" "${timer}"; then
        echo "The display timer is stopped. Start it with: sudo systemctl start ${timer}" >&2
      fi
    fi
    if [ -n "${installed}" ] && [ "${previous_kept}" = true ]; then
      echo "The upgrade failed; the previous version is unchanged." >&2
    elif [ -n "${installed}" ]; then
      echo "The upgrade failed after the new version was installed; the display uses the new version." >&2
    fi
  fi
  rm -rf "${work_dir}"
  exit "${status}"
}
trap cleanup EXIT

chmod 600 "${config_path}"
mkdir -p "${app_dir}/catalog" "${app_dir}/deploy" "${runtimes_dir}" "${support_dir}"
runtimes_dir=$(cd -P "${runtimes_dir}" && pwd)
if [ -n "${installed}" ]; then
  installed_source=$("${venv}/bin/python" -I -c "${installed_source_program}") || installed_source=""
fi

# Stage and check the new runtime beside the live one; nothing running changes.
new_runtime=$(mktemp -d "${runtimes_dir}/runtime.XXXXXX")
rsync -a --delete "${root}/src/" "${new_runtime}/src/"
mkdir -p "${new_runtime}/deploy" "${new_runtime}/docs"
install -m 0755 "${root}/deploy/install-display-local.sh" "${new_runtime}/deploy/"
for file in pyproject.toml uv.lock README.md LICENSE docs/browser-access.md; do
  install -m 0644 "${root}/${file}" "${new_runtime}/${file}"
done
if ! "${venv}/bin/python" -I -c "${validate_program}" "${config_path}" "${new_runtime}/src"; then
  echo "The new version cannot use this configuration. No changes made." >&2
  exit 1
fi

if systemctl is-enabled --quiet "${timer}"; then
  timer_was_enabled=true
fi
for name in "${service_prefix}-display.service" "${timer}"; do
  if [ -f "${unit_root}/${name}" ]; then
    cp "${unit_root}/${name}" "${unit_backup_dir}/${name}"
  fi
done

# A failed install leaves the old distribution in place: pip restores a
# same-named one itself, and the renamed one is not touched until later.
"${venv}/bin/python" -m pip install --disable-pip-version-check -e "${new_runtime}[inky]"
package_installed=true

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
executable = venv / "bin/inky-bird"
config = load_config(config_path)
units = display_systemd_units(
    config,
    executable=executable,
    app_dir=app_dir,
    config_path=config_path,
    user=user,
    service_prefix=os.environ["INKY_BIRD_SERVICE_PREFIX"],
)
for name, content in units.items():
    (unit_dir / name).write_text(content)
PY

# The new distribution now owns the shared files; remove the rest of the old one.
if [ "${installed}" = inky-bird-frame ]; then
  old_package_removed=true
  "${venv}/bin/python" -I -c "${remove_distribution_program}" inky-bird-frame inky-bird
fi

# ${app_dir}/src links to the active runtime's source; the single-directory
# layout becomes the previous runtime, complete enough to reinstall from.
runtime_linked=true
if [ -L "${app_dir}/src" ]; then
  source_link=$(readlink "${app_dir}/src")
elif [ -e "${app_dir}/src" ]; then
  moved_runtime=$(mktemp -d "${runtimes_dir}/runtime.XXXXXX")
  mkdir -p "${moved_runtime}/docs"
  for file in pyproject.toml uv.lock README.md LICENSE docs/browser-access.md; do
    if [ -f "${app_dir}/${file}" ]; then
      cp -p "${app_dir}/${file}" "${moved_runtime}/${file}"
    fi
  done
  mv "${app_dir}/src" "${moved_runtime}/src"
fi
if [ -n "${installed_source}" ] && [ "$(dirname "${installed_source}")" = "${runtimes_dir}" ]; then
  previous_runtime=${installed_source}
else
  previous_runtime=${moved_runtime}
fi
link_source "${new_runtime}/src"

if [ "${root}" != "${app_dir}" ]; then
  mkdir -p "${app_dir}/docs"
  rsync -a "${root}/catalog/" "${app_dir}/catalog/"
  install -m 0755 "${root}/deploy/install-display-local.sh" "${app_dir}/deploy/"
  for file in pyproject.toml uv.lock README.md LICENSE docs/browser-access.md; do
    install -m 0644 "${root}/${file}" "${app_dir}/${file}"
  done
fi

units_replaced=true
"${sudo_command[@]}" install -m 0644 \
  "${unit_dir}/${service_prefix}-display.service" \
  "${unit_root}/${service_prefix}-display.service"
"${sudo_command[@]}" install -m 0644 \
  "${unit_dir}/${service_prefix}-display.timer" \
  "${unit_root}/${service_prefix}-display.timer"
"${sudo_command[@]}" systemctl daemon-reload
"${sudo_command[@]}" systemctl enable "${service_prefix}-display.timer"
"${sudo_command[@]}" systemctl restart "${service_prefix}-display.timer"
if [ "${run_initial_display}" = true ]; then
  "${sudo_command[@]}" systemctl start "${service_prefix}-display.service"
fi
systemctl is-enabled --quiet "${service_prefix}-display.timer"
systemctl is-active --quiet "${service_prefix}-display.timer"

installation_complete=true
# Keep the previous runtime for rollback; remove anything older.
for runtime in "${runtimes_dir}"/*; do
  if [ -d "${runtime}" ] && [ "${runtime}" != "${new_runtime}" ] && \
      [ "${runtime}" != "${previous_runtime}" ]; then
    rm -rf "${runtime}"
  fi
done
echo "Display node installed from ${root} into ${app_dir}."
echo "Configuration: ${config_path}"
if [ -n "${previous_runtime}" ]; then
  echo "Previous runtime kept for rollback: ${previous_runtime}"
fi
