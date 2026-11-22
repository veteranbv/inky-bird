"""Generate per-scene narration WAVs for the demo film.

Default engine is Kokoro (Apache-2.0, offline, CPU) through kokoro-onnx, so a
clean checkout needs no account and no key. If ELEVENLABS_API_KEY is present
in the environment and --engine elevenlabs is requested, that service is used
instead; the key is read only from the environment and never printed, logged
or written anywhere.

Run inside the dedicated voice environment (see demo/make-demo.sh):

    python demo/voice/synthesize.py --storyboard demo/storyboard.json --out <dir>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

MODEL_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
MODEL_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")


def ensure_models(models_dir: Path) -> tuple[Path, Path]:
    models_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in MODEL_FILES:
        path = models_dir / name
        if not path.exists():
            print(f"Downloading {name} (one-time, Apache-2.0 model files) ...", flush=True)
            temporary = path.with_suffix(path.suffix + ".download")
            urllib.request.urlretrieve(f"{MODEL_RELEASE}/{name}", temporary)  # noqa: S310
            temporary.replace(path)
        paths.append(path)
    return paths[0], paths[1]


def synthesize_kokoro(
    scenes: list[dict], out_dir: Path, models_dir: Path, voice: str, speed: float
) -> dict[str, float]:
    import soundfile  # noqa: PLC0415
    from kokoro_onnx import Kokoro  # noqa: PLC0415

    model, voices = ensure_models(models_dir)
    kokoro = Kokoro(str(model), str(voices))
    durations: dict[str, float] = {}
    for scene in scenes:
        samples, sample_rate = kokoro.create(scene["narration"], voice=voice, speed=speed)
        path = out_dir / f"{scene['id']}.wav"
        soundfile.write(path, samples, sample_rate)
        durations[scene["id"]] = round(len(samples) / sample_rate, 3)
        print(f"voice {scene['id']}: {durations[scene['id']]:.2f}s", flush=True)
    return durations


def synthesize_elevenlabs(scenes: list[dict], out_dir: Path) -> dict[str, float]:
    """Optional paid engine; used only on explicit request with a key present."""
    import numpy  # noqa: PLC0415
    import soundfile  # noqa: PLC0415

    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise SystemExit("--engine elevenlabs needs ELEVENLABS_API_KEY in the environment")
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
    durations: dict[str, float] = {}
    for scene in scenes:
        body = json.dumps(
            {"text": scene["narration"], "model_id": "eleven_multilingual_v2"}
        ).encode()
        request = urllib.request.Request(  # noqa: S310
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=pcm_24000",
            data=body,
            headers={"xi-api-key": key, "content-type": "application/json"},
        )
        with urllib.request.urlopen(request) as answer:  # noqa: S310
            pcm = answer.read()
        samples = numpy.frombuffer(pcm, dtype=numpy.int16).astype(numpy.float32) / 32768.0
        path = out_dir / f"{scene['id']}.wav"
        soundfile.write(path, samples, 24000)
        durations[scene["id"]] = round(len(samples) / 24000, 3)
        print(f"voice {scene['id']}: {durations[scene['id']]:.2f}s", flush=True)
    return durations


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storyboard", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--engine", choices=["kokoro", "elevenlabs"], default="kokoro")
    parser.add_argument("--models-dir", type=Path, default=Path(__file__).parent / "models")
    arguments = parser.parse_args()
    storyboard = json.loads(arguments.storyboard.read_text())
    scenes = [scene for scene in storyboard["scenes"] if scene.get("narration")]
    arguments.out.mkdir(parents=True, exist_ok=True)
    if arguments.engine == "kokoro":
        voice = storyboard["voice"].get("kokoroVoice", "af_heart")
        speed = storyboard["voice"].get("speed", 1.0)
        durations = synthesize_kokoro(scenes, arguments.out, arguments.models_dir, voice, speed)
    else:
        durations = synthesize_elevenlabs(scenes, arguments.out)
    (arguments.out / "narration-durations.json").write_text(json.dumps(durations, indent=2) + "\n")


if __name__ == "__main__":
    sys.exit(main())
