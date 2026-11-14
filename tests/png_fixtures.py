"""Small real PNG files for tests whose code under test decodes candidate images."""

from __future__ import annotations

from pathlib import Path

from PIL import Image


def write_png(
    path: Path,
    *,
    size: tuple[int, int] = (4, 3),
    color: tuple[int, int, int] = (120, 90, 60),
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")
    return path


def write_plate_pair(directory: Path) -> None:
    write_png(directory / "portrait.png", color=(200, 40, 40))
    write_png(directory / "display.png", color=(40, 40, 200))


def write_truncated_png(path: Path, *, size: tuple[int, int] = (64, 64)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Noise keeps the compressed data long enough that a cut lands inside it.
    Image.effect_noise(size, 64).convert("RGB").save(path, format="PNG")
    content = path.read_bytes()
    path.write_bytes(content[: len(content) // 2])
    return path
