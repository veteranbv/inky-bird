from __future__ import annotations

import re
from pathlib import Path

from starlette.testclient import TestClient

from inky_bird_frame.config import ControllerConfig
from inky_bird_frame.web import create_app

WEB = Path(__file__).parents[1] / "src/inky_bird_frame/web"


def _config(root: Path) -> ControllerConfig:
    return ControllerConfig(
        root / "work",
        root / "catalog",
        root / "state",
        Path("codex"),
        None,
        "127.0.0.1",
        8000,
        3,
        1,
        3,
        owner_allowed_origins=("https://inky.example.test",),
    )


def test_every_file_the_owner_page_names_is_served(tmp_path: Path) -> None:
    page = (WEB / "index.html").read_text()
    named = set(re.findall(r'(?:src|href)="(/app/[^"]+)"', page))
    assert "/app/app.js" in named and "/app/app.css" in named
    with TestClient(create_app(_config(tmp_path))) as client:
        missing = sorted(path for path in named if client.get(path).status_code != 200)
    assert missing == []
