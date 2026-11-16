from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest

from inky_bird_frame.config import AppConfig, load_config
from inky_bird_frame.imports import HistoryImports
from inky_bird_frame.owner_operations import OwnerOperationError
from inky_bird_frame.settings import initialize_config, settings_status

HEADER = b"Start (s),End (s),Scientific name,Common name,Confidence,File\n"
BLUEBIRD = b"0,3,Sialia sialis,Eastern Bluebird,0.91,fixture.wav\n"
CARDINAL = b"0,3,Cardinalis cardinalis,Northern Cardinal,0.62,fixture.wav\n"


def configuration(root: Path) -> AppConfig:
    path = root / "config.toml"
    initialize_config(path)
    return load_config(path, load_secrets=False)


def preview_analyzer(
    imports: HistoryImports, content: bytes, revision: str
) -> tuple[str, dict[str, Any]]:
    identifier, handle = imports.begin("birdnet-analyzer")
    with handle:
        handle.write(content)
    imports.finish_upload(identifier, hashlib.sha256(content).hexdigest(), len(content))
    imports.preview(identifier, revision=revision)
    preview = cast(dict[str, Any], imports.status()["preview"])
    return identifier, cast(dict[str, Any], preview["preview"])


def analyzer_species(config: AppConfig) -> list[str]:
    path = config.controller.state_dir / "birdnet-analyzer-detections.json"
    detections = json.loads(path.read_text())["detections"].values()
    return sorted(item["scientific_name"] for item in detections)


def test_analyzer_removal_needs_the_same_acknowledgement_the_page_sends(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    imports = HistoryImports(config)
    revision = str(settings_status(config)["revision"])
    identifier, preview = preview_analyzer(imports, HEADER + BLUEBIRD + CARDINAL, revision)
    imports.commit(
        identifier,
        revision=revision,
        preview_hash=preview["preview_hash"],
        key="first-analyzer-import",
        allow_reduction=False,
    )
    imports.cancel(identifier)

    identifier, preview = preview_analyzer(imports, HEADER + BLUEBIRD, revision)
    assert preview["stats"]["removed"] == 1
    with pytest.raises(OwnerOperationError, match="^Confirm the removal"):
        imports.commit(
            identifier,
            revision=revision,
            preview_hash=preview["preview_hash"],
            key="unacknowledged-removal",
            allow_reduction=False,
        )
    assert analyzer_species(config) == ["Cardinalis cardinalis", "Sialia sialis"]

    result = imports.commit(
        identifier,
        revision=revision,
        preview_hash=preview["preview_hash"],
        key="acknowledged-removal",
        allow_reduction=True,
    )
    assert result["removed"] == 1
    assert analyzer_species(config) == ["Sialia sialis"]
