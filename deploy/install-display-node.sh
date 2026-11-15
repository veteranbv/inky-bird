#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
display_host=${INKY_BIRD_DISPLAY_HOST:?Set INKY_BIRD_DISPLAY_HOST to the display node address}
display_user=${INKY_BIRD_DISPLAY_USER:?Set INKY_BIRD_DISPLAY_USER to the SSH user}
ssh_key=${INKY_BIRD_DISPLAY_SSH_KEY:?Set INKY_BIRD_DISPLAY_SSH_KEY to the SSH private key path}
remote_app=${INKY_BIRD_DISPLAY_APP_DIR:?Set INKY_BIRD_DISPLAY_APP_DIR to the remote application directory}
remote_config=${INKY_BIRD_DISPLAY_CONFIG_PATH:?Set INKY_BIRD_DISPLAY_CONFIG_PATH to the remote config path}
remote_venv=${INKY_BIRD_DISPLAY_VENV:?Set INKY_BIRD_DISPLAY_VENV to the remote Python environment}
run_initial_display=${INKY_BIRD_RUN_INITIAL_DISPLAY:-false}
remote="${display_user}@${display_host}"
ssh_options=(-i "${ssh_key}" -o BatchMode=yes -o IdentitiesOnly=yes)

if [ ! -f "${ssh_key}" ]; then
  echo "Display deployment key is missing: ${ssh_key}" >&2
  exit 1
fi

for path in "${remote_app}" "${remote_config}" "${remote_venv}"; do
  if [[ "${path}" != /* || "${path}" == *$'\n'* || "${path}" == *$'\r'* ]]; then
    echo "Remote application, config and venv paths must be absolute, single-line paths." >&2
    exit 1
  fi
done
case "${run_initial_display}" in
  true | false) ;;
  *) echo "INKY_BIRD_RUN_INITIAL_DISPLAY must be true or false." >&2; exit 1 ;;
esac

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

remote_command() {
  local command
  printf -v command '%q ' "$@"
  # shellcheck disable=SC2029 # Fixed arguments are individually escaped above.
  ssh "${ssh_options[@]}" "${remote}" "${command}"
}

# Never stage over an installed runtime. The local installer must get to run
# its identity, quiescence and authorization checks before any live copy.
remote_parent=${remote_app%/*}
remote_parent=${remote_parent:-/}
stage_prefix="${remote_parent%/}/.inky-bird-stage."
source_kib=$(du -sk "${root}/src" "${root}/catalog" "${root}/deploy" \
  "${root}/docs/browser-access.md" "${root}/pyproject.toml" "${root}/uv.lock" \
  "${root}/README.md" "${root}/LICENSE" | awk '{sum += $1} END {printf "%.0f", sum}')
# Cover the stage and a complete fresh managed copy, plus modest headroom.
required_kib=$((source_kib * 2 + 65536))
# Name the stage here so cleanup knows it even if the reply is lost.
stage_suffix=$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')
if [[ ! "${stage_suffix}" =~ ^[0-9a-f]{12}$ ]]; then
  echo "Could not choose a private staging name. No changes made." >&2
  exit 1
fi
remote_stage="${stage_prefix}${stage_suffix}"
reply_dir=""
stage_requested=false
stage_foreign=false
cleanup() {
  status=$?
  trap - EXIT
  # Finish cleaning up even if a stop signal arrives, then report it.
  cleanup_stop=""
  trap 'cleanup_stop=129' HUP
  trap 'cleanup_stop=130' INT
  trap 'cleanup_stop=143' TERM
  if [ -n "${reply_dir}" ] && ! rm -rf "${reply_dir}"; then
    echo "Could not remove temporary files; remove them by hand: ${reply_dir}" >&2
  fi
  # Only the exact path this invocation asked to create, never one that existed.
  if [ "${stage_requested}" = true ] && [ "${stage_foreign}" != true ] && \
      ! remote_command rm -rf -- "${remote_stage}"; then
    echo "Could not remove isolated public-source staging; remove it after reconnecting: ${remote_stage}" >&2
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
make_temp_dir reply_dir
stage_requested=true
stage_status=0
remote_command bash -s -- "${remote_parent}" "${remote_stage}" \
  "${required_kib}" >"${reply_dir}/stage" <<'REMOTE' || stage_status=$?
set -euo pipefail
parent=$1
probe=${parent}
while [ ! -d "${probe}" ]; do
  probe=${probe%/*}
  probe=${probe:-/}
done
available=$(df -Pk "${probe}" | awk 'NR == 2 {print $4}')
if [ -z "${available}" ] || [ -n "${available//[0-9]/}" ]; then
  echo "Cannot determine remote staging capacity." >&2
  exit 1
fi
if [ "${available}" -lt "$3" ]; then
  echo "Not enough remote capacity for isolated source staging and the managed copy: need $3 KiB, have ${available} KiB. No live runtime changes made." >&2
  exit 1
fi
mkdir -p "${parent}"
# mkdir refuses an existing path atomically; never reuse one.
if ! mkdir -m 700 "$2" 2>/dev/null; then
  if [ -e "$2" ] || [ -L "$2" ]; then
    exit 17
  fi
  echo "Cannot create remote staging directory." >&2
  exit 1
fi
printf '%s\n' "$2"
REMOTE
if [ "${stage_status}" -eq 17 ]; then
  stage_foreign=true
  echo "Remote staging path already exists; run the deployment again. No changes made." >&2
  exit 1
fi
if [ "${stage_status}" -ne 0 ]; then
  exit "${stage_status}"
fi
if [ "$(cat "${reply_dir}/stage")" != "${remote_stage}" ]; then
  echo "Remote staging did not return a recognized private temporary directory." >&2
  exit 1
fi
remote_command mkdir -p "${remote_stage}/src" "${remote_stage}/catalog" \
  "${remote_stage}/deploy" "${remote_stage}/docs"
# rsync parses -e itself: quotes group spaces, doubled quotes escape a quote,
# and backslashes are literal (unlike Bash's %q output).
rsync_shell=''
for argument in ssh "${ssh_options[@]}"; do
  rsync_shell+="\"${argument//\"/\"\"}\" "
done
rsync -a --delete -e "${rsync_shell}" "${root}/src/" "${remote}:${remote_stage}/src/"
rsync -a -e "${rsync_shell}" "${root}/catalog/" "${remote}:${remote_stage}/catalog/"
for file in pyproject.toml uv.lock README.md LICENSE docs/browser-access.md; do
  rsync -a -e "${rsync_shell}" "${root}/${file}" "${remote}:${remote_stage}/${file}"
done
rsync -a -e "${rsync_shell}" \
  "${root}/deploy/install-display-local.sh" "${remote}:${remote_stage}/deploy/"

remote_command bash -s -- "${remote_stage}" \
  "${remote_app}" "${remote_config}" "${remote_venv}" "${run_initial_display}" <<'REMOTE'
set -euo pipefail
source_dir=$1
app_dir=$2
config_path=$3
venv=$4
INKY_BIRD_APP_DIR="${app_dir}" \
INKY_BIRD_CONFIG_PATH="${config_path}" \
INKY_BIRD_DISPLAY_VENV="${venv}" \
INKY_BIRD_RUN_INITIAL_DISPLAY="$5" \
INKY_BIRD_NONINTERACTIVE_SUDO=true \
  "${source_dir}/deploy/install-display-local.sh"
REMOTE

echo "Display node deployed to ${remote}."
