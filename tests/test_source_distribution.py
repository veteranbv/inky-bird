"""The source distribution leaves out the plate catalog and still builds the wheel."""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _build(*args: str, out: Path) -> Path:
    uv = shutil.which("uv")
    assert uv is not None
    subprocess.run(
        [uv, "build", "--offline", *args, "--out-dir", str(out)],
        cwd=out.parent,
        check=True,
        capture_output=True,
        timeout=300,
    )
    return next(path for path in out.iterdir() if path.suffix in {".gz", ".whl"})


def test_sdist_excludes_the_catalog_and_builds_the_wheel(tmp_path: Path) -> None:
    sdist = _build("--sdist", str(ROOT), out=tmp_path / "sdist")
    with tarfile.open(sdist) as archive:
        names = [name.split("/", 1)[1] for name in archive.getnames() if "/" in name]
    assert "src/inky_bird_frame/__init__.py" in names
    assert "docs/browser-access.md" in names
    assert not [name for name in names if name == "catalog" or name.startswith("catalog/")]

    wheel = _build("--wheel", str(sdist), out=tmp_path / "wheel")
    with zipfile.ZipFile(wheel) as archive:
        assert "inky_bird_frame/web/access-guide.txt" in archive.namelist()
