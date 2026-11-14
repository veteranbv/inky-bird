from __future__ import annotations

import hashlib
import io
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from starlette.testclient import TestClient

from inky_bird_frame.access import AccessStore
from inky_bird_frame.config import ControllerConfig
from inky_bird_frame.http import write_json_atomic
from inky_bird_frame.journal import JournalService
from inky_bird_frame.web import create_app

ORIGIN = "https://inky.example.test"
STAMP = "2026-10-15T12:00:00+00:00"
PASSWORD = "a synthetic long passphrase"
IMMUTABLE = "private, max-age=31536000, immutable"


def _plate(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    # Noise keeps the PNG near the size of a real plate rather than a flat fill.
    noise = Image.effect_noise((1200, 1600), 48).convert("RGB")
    Image.blend(noise, Image.new("RGB", (1200, 1600), color), 0.5).save(buffer, format="PNG")
    return buffer.getvalue()


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
        owner_allowed_origins=(ORIGIN,),
    )


def _publish(config: ControllerConfig, portrait: bytes) -> str:
    species = config.catalog_dir / "species/1-robin"
    species.mkdir(parents=True, exist_ok=True)
    (species / "portrait.png").write_bytes(portrait)
    (species / "display.png").write_bytes(portrait)
    digest = hashlib.sha256(portrait).hexdigest()
    write_json_atomic(
        config.catalog_dir / "index.json",
        {
            "schema_version": 1,
            "generated_at": STAMP,
            "species": [
                {
                    "taxon_id": 1,
                    "common_name": "Robin",
                    "scientific_name": "Turdus migratorius",
                    "slug": "robin",
                    "portrait_path": "species/1-robin/portrait.png",
                    "display_path": "species/1-robin/display.png",
                    "portrait_sha256": digest,
                    "display_sha256": digest,
                    "approved_at": STAMP,
                }
            ],
        },
    )
    return digest


def _catalog_files(config: ControllerConfig) -> dict[str, bytes]:
    return {
        str(path.relative_to(config.catalog_dir)): path.read_bytes()
        for path in sorted(config.catalog_dir.rglob("*"))
        if path.is_file()
    }


def _signed_in(client: TestClient, config: ControllerConfig) -> None:
    AccessStore(config.state_dir).setup_owner("qa-owner", PASSWORD)
    response = client.post(
        "/v1/owner/login",
        headers={"Origin": ORIGIN},
        json={"username": "qa-owner", "passphrase": PASSWORD},
    )
    assert response.status_code == 200, response.text


def test_thumbnail_is_a_small_private_cached_copy_of_the_verified_plate(tmp_path: Path) -> None:
    config = _config(tmp_path)
    portrait = _plate((200, 40, 40))
    digest = _publish(config, portrait)
    before = _catalog_files(config)
    with TestClient(create_app(config), base_url=ORIGIN) as client:
        denied = client.get(f"/v1/owner/journal/birds/1/thumbnail?revision={digest}")
        assert denied.status_code == 401
        assert denied.headers["cache-control"] == "no-store"
        _signed_in(client, config)
        response = client.get(f"/v1/owner/journal/birds/1/thumbnail?revision={digest}")
        assert response.status_code == 200
        assert response.headers["cache-control"] == IMMUTABLE
        assert response.headers["content-type"] in {"image/jpeg", "image/webp"}
        assert len(response.content) * 4 < len(portrait)
        with Image.open(io.BytesIO(response.content)) as image:
            assert image.format == response.headers["content-type"].split("/")[1].upper()
            assert image.size == (480, 640)
        cached = list((config.state_dir / "plate-thumbnails").iterdir())
        assert [path.name.split(".")[0] for path in cached] == [digest]
        assert cached[0].stat().st_mode & 0o777 == 0o600
        assert cached[0].read_bytes() == response.content
        # A cached copy is served again; the approved catalog is never written.
        assert client.get("/v1/owner/journal/birds/1/thumbnail").content == response.content
    assert _catalog_files(config) == before


def test_only_the_current_revision_is_cacheable(tmp_path: Path) -> None:
    config = _config(tmp_path)
    portrait = _plate((40, 200, 40))
    digest = _publish(config, portrait)
    with TestClient(create_app(config), base_url=ORIGIN) as client:
        _signed_in(client, config)
        for kind in ("portrait", "thumbnail"):
            route = f"/v1/owner/journal/birds/1/{kind}"
            current = client.get(f"{route}?revision={digest}")
            assert current.headers["cache-control"] == IMMUTABLE
            for stale in (
                route,
                f"{route}?revision={'b' * 64}",
                f"{route}?revision={digest}&revision={digest}",
            ):
                response = client.get(stale)
                assert response.status_code == 200
                assert response.headers["cache-control"] == "no-store"
                assert response.content == current.content
        full = client.get(f"/v1/owner/journal/birds/1/portrait?revision={digest}")
        assert full.headers["content-type"] == "image/png"
        assert full.content == portrait
        # Other owner responses never become cacheable.
        assert client.get("/v1/owner/journal/birds/1/profile").headers["cache-control"] == (
            "no-store"
        )
        assert client.get("/v1/owner/journal/birds/1/other").status_code == 404


def test_replaced_plates_drop_their_old_thumbnail(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first = _publish(config, _plate((40, 40, 200)))
    with TestClient(create_app(config), base_url=ORIGIN) as client:
        _signed_in(client, config)
        assert (
            client.get(f"/v1/owner/journal/birds/1/thumbnail?revision={first}").status_code == 200
        )
        cache = config.state_dir / "plate-thumbnails"
        leftover = cache / ".interrupted-write.tmp"
        leftover.write_bytes(b"partial")
        second = _publish(config, _plate((200, 200, 40)))
        stale = client.get(f"/v1/owner/journal/birds/1/thumbnail?revision={first}")
        assert stale.headers["cache-control"] == "no-store"
        assert [path.name.split(".")[0] for path in cache.iterdir()] == [second]


def test_a_plate_replaced_mid_request_is_never_cached_as_the_old_revision(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    initial = _plate((40, 40, 200))
    first = _publish(config, initial)
    replacement = _plate((200, 200, 40))
    second = hashlib.sha256(replacement).hexdigest()
    original = JournalService.portrait

    def replace_then_read(self: JournalService, taxon_id: int) -> bytes:
        # The request named the first revision; the plate changes before its bytes are read.
        _publish(config, replacement)
        return original(self, taxon_id)

    with TestClient(create_app(config), base_url=ORIGIN) as client:
        _signed_in(client, config)
        for kind in ("portrait", "thumbnail"):
            _publish(config, initial)
            with patch.object(JournalService, "portrait", replace_then_read):
                response = client.get(f"/v1/owner/journal/birds/1/{kind}?revision={first}")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            current = client.get(f"/v1/owner/journal/birds/1/{kind}?revision={second}")
            assert current.headers["cache-control"] == IMMUTABLE
            assert response.content == current.content
        # The cached thumbnail is named for the plate it was made from.
        cache = config.state_dir / "plate-thumbnails"
        assert [path.name.split(".")[0] for path in cache.iterdir()] == [second]
        with Image.open(next(cache.iterdir())) as image:
            pixel = image.convert("RGB").resize((1, 1)).getpixel((0, 0))
        assert isinstance(pixel, tuple)
        red, green, blue = pixel
        assert red > blue and green > blue


def test_an_undecodable_plate_fails_without_revealing_paths(tmp_path: Path) -> None:
    config = _config(tmp_path)
    digest = _publish(config, b"\x89PNG\r\n\x1a\nnot really an image")
    with TestClient(create_app(config), base_url=ORIGIN) as client:
        _signed_in(client, config)
        response = client.get(f"/v1/owner/journal/birds/1/thumbnail?revision={digest}")
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
        assert str(tmp_path) not in response.text
        assert not (config.state_dir / "plate-thumbnails").exists() or not any(
            (config.state_dir / "plate-thumbnails").iterdir()
        )


def test_static_app_files_revalidate_instead_of_downloading_again(tmp_path: Path) -> None:
    with TestClient(create_app(_config(tmp_path))) as client:
        for path in ("/app", "/app/app.js", "/app/journal.js", "/app/app.css"):
            first = client.get(path)
            assert first.status_code == 200
            assert first.headers["cache-control"] == "no-cache"
            etag = first.headers["etag"]
            again = client.get(path, headers={"If-None-Match": etag})
            assert again.status_code == 304
            assert again.content == b""
            assert again.headers["cache-control"] == "no-cache"
            assert "frame-ancestors 'none'" in again.headers["content-security-policy"]
            changed = client.get(path, headers={"If-None-Match": '"other"'})
            assert changed.status_code == 200 and changed.content == first.content


def test_owner_page_preloads_every_module_it_imports() -> None:
    web = Path(__file__).parents[1] / "src/inky_bird_frame/web"
    page = (web / "index.html").read_text()
    imported = set()
    for module in ("app.js", "journal.js"):
        for line in (web / module).read_text().splitlines():
            if line.startswith("import ") and "'/app/" in line:
                imported.add(line.split("'")[1])
    assert imported
    for module in imported:
        assert f'<link rel="modulepreload" href="{module}">' in page
