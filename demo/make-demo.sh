#!/usr/bin/env bash
# Regenerate the Inky Bird demo film, README GIF and review bundle from a
# clean checkout, with only free local tools. See demo/README.md.
#
#   demo/make-demo.sh [--out <dir>] [--no-voice] [--scene <id>]... [--stills-only]
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="$root/demo/out"
no_voice=""
stills_only=""
scenes=()
while [ $# -gt 0 ]; do
  case "$1" in
    --out) out="$2"; shift 2 ;;
    --no-voice) no_voice=1; shift ;;
    --stills-only) stills_only=1; shift ;;
    --scene) scenes+=("--scene" "$2"); shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
mkdir -p "$out"
out=$(cd "$out" && pwd)

for tool in node ffmpeg ffprobe; do
  command -v "$tool" >/dev/null || { echo "Missing $tool" >&2; exit 1; }
done
node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 20 ? 0 : 1)' \
  || { echo "Node.js 20 or newer is required" >&2; exit 1; }
[ -x "${INKY_QA_PYTHON:-$root/.venv/bin/python}" ] \
  || { echo "Project .venv missing; run uv sync --extra dev --locked first (see CONTRIBUTING.md)" >&2; exit 1; }

echo "== 1/5 capture (real app, loopback, synthetic fixture)"
node "$root/tests/web/capture-demo.cjs" "$out/capture" ${scenes[@]+"${scenes[@]}"}

if [ -z "$no_voice" ]; then
  engine="Kokoro, offline"
  [ -n "${ELEVENLABS_API_KEY:-}" ] && [ -n "${INKY_DEMO_ELEVENLABS:-}" ] && engine="ElevenLabs"
  echo "== 2/5 narration (${engine})"
  voice_python="$root/demo/voice/.venv/bin/python"
  if [ ! -x "$voice_python" ]; then
    if command -v uv >/dev/null; then
      uv venv "$root/demo/voice/.venv" >/dev/null
      uv pip install --python "$voice_python" kokoro-onnx soundfile >/dev/null
    else
      python3 -m venv "$root/demo/voice/.venv"
      "$voice_python" -m pip install --quiet kokoro-onnx soundfile
    fi
  fi
  "$voice_python" "$root/demo/voice/synthesize.py" \
    --storyboard "$root/demo/storyboard.json" --out "$out/voice" \
    ${ELEVENLABS_API_KEY:+${INKY_DEMO_ELEVENLABS:+--engine elevenlabs}}
else
  echo "== 2/5 narration skipped (--no-voice)"
fi

echo "== 3/5 timeline"
node "$root/demo/compose-timeline.cjs" "$out" ${no_voice:+--no-voice}

echo "== 4/5 compose (Remotion)"
assets="$root/demo/video/public/demo"
mkdir -p "$assets" "$assets/audio"
cp "$out"/capture/*.webm "$assets/"
cp "$root/catalog/species/9083-northern-cardinal/portrait.png" "$assets/portrait.png"
cp "$root/docs/images/framed-installation.jpg" "$assets/framed-installation.jpg"
cp "$root/docs/images/inky-bird-mark.png" "$assets/inky-bird-mark.png"
if [ -z "$no_voice" ]; then cp "$out"/voice/*.wav "$assets/audio/"; fi
if [ ! -d "$root/demo/video/node_modules" ]; then
  (cd "$root/demo/video" && npm ci --no-audit --no-fund)
fi
(cd "$root/demo/video" && node tools/render.mjs --timeline "$out/timeline.json" --out "$out" \
  ${stills_only:+--stills-only} ${scenes[@]+"${scenes[@]}"})

if [ -n "$stills_only" ] || [ "${#scenes[@]}" -gt 0 ]; then
  echo "Stills only: $out/stills"
  exit 0
fi

echo "== 5/5 export and review"
node "$root/demo/export.cjs" "$out" ${no_voice:+--no-voice}
