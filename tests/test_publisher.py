from __future__ import annotations

import io
import json
import os
import shutil
import stat
import struct
import subprocess
import unittest
import zlib
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import call, patch

import pytest
from PIL import Image, PngImagePlugin

from inky_bird_frame.catalog import rebuild_catalog_index, sha256_file
from inky_bird_frame.cli import build_parser, catalog_sync_command
from inky_bird_frame.config import AppConfig, PublicCatalogConfig, load_config
from inky_bird_frame.errors import CatalogPublishError
from inky_bird_frame.http import write_json_atomic
from inky_bird_frame.publisher import (
    _COMMAND_TIMEOUT_SECONDS,
    _abandoned_publish_work,
    _catalog_transaction_prefix,
    _check_json_privacy,
    _cleanup_abandoned_publish_work_locked,
    _coordinate_terms,
    _gh,
    _git,
    _remote_repository,
    _run,
    _validate_checkout,
    _validate_image,
    publication_privacy_patterns,
    replace_public_catalog_taxon,
    run_catalog_publish,
    sync_public_catalog,
    validate_catalog_additions,
    validate_public_catalog,
)


def _run_git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _create_species(catalog: Path, taxon_id: int, common_name: str) -> Path:
    slug = common_name.casefold().replace(" ", "-")
    directory = catalog / "species" / f"{taxon_id}-{slug}"
    directory.mkdir(parents=True)
    Image.new("RGB", (1200, 1600), "white").save(directory / "portrait.png")
    Image.new("RGB", (1600, 1200), "white").save(directory / "display.png")
    profile = {
        "taxon_id": taxon_id,
        "common_name": common_name,
        "scientific_name": "Avis exemplaris",
        "family": "Exemplaridae",
        "measurements": {"length": "10 cm", "wingspan": "20 cm", "weight": "10 g"},
        "field_marks": ["example field mark"],
        "habitat": "Woodland",
        "behavior": "Forages",
        "palette": ["white"],
        "sources": [
            {"title": "Source one", "url": "https://example.test/one"},
            {"title": "Source two", "url": "https://example.test/two"},
        ],
    }
    review = {
        "passed": True,
        "species_accuracy": 5,
        "anatomy_accuracy": 5,
        "text_accuracy": 5,
        "composition_quality": 5,
        "location_free": True,
        "findings": [],
        "verification_sources": [
            {"title": "Source one", "url": "https://example.test/one"},
            {"title": "Source two", "url": "https://example.test/two"},
        ],
    }
    write_json_atomic(directory / "profile.json", profile)
    write_json_atomic(directory / "quality-review.json", review)
    write_json_atomic(
        directory / "manifest.json",
        {
            "schema_version": 1,
            "status": "approved",
            "taxon_id": taxon_id,
            "common_name": common_name,
            "scientific_name": "Avis exemplaris",
            "slug": slug,
            "approved_at": "2026-07-09T00:00:00+00:00",
            "profile": profile,
            "references": [],
            "generation": {
                "generator": "test",
                "prompt_version": "test-v1",
                "generated_at": "2026-07-09T00:00:00+00:00",
                "attempt": 1,
                "max_attempts": 3,
            },
            "quality_review": review,
            "assets": {
                "portrait": {
                    "filename": "portrait.png",
                    "sha256": sha256_file(directory / "portrait.png"),
                },
                "display": {
                    "filename": "display.png",
                    "sha256": sha256_file(directory / "display.png"),
                },
            },
        },
    )
    rebuild_catalog_index(catalog)
    return directory


def _create_portrait_replacement(
    catalog: Path,
    taxon_id: int,
    common_name: str,
    *,
    approved_at: str,
    color: str,
) -> Path:
    directory = _create_species(catalog, taxon_id, common_name)
    portrait = directory / "portrait.png"
    Image.new("RGB", (1200, 1600), color).save(portrait)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["approved_at"] = approved_at
    manifest["assets"]["portrait"]["sha256"] = sha256_file(portrait)
    write_json_atomic(manifest_path, manifest)
    rebuild_catalog_index(catalog)
    return directory


def _write_config(root: Path, checkout: Path, *, location: str = 'zip_code = "12345"') -> Path:
    path = root / "config.toml"
    path.write_text(
        f"""
[discovery]
{location}
radius_km = 8
species_limit = 12
window = "last-week"

[controller]
workspace_dir = "."
catalog_dir = "catalog"
state_dir = "state"
codex_path = "/usr/bin/false"
bind_host = "127.0.0.1"
port = 8793
references_per_species = 4
generations_per_cycle = 1
max_generation_attempts = 3

[display_node]
controller_url = "http://controller.test:8793"
state_dir = "display"

[public_catalog]
enabled = true
checkout_dir = "{checkout}"
repository = "example/inky-bird-frame"
gh_path = "/usr/bin/false"
remote = "origin"
base_branch = "main"
commit_name = "Test Publisher"
commit_email = "publisher@example.test"
"""
    )
    return path


def _initialize_remote(root: Path) -> tuple[Path, Path]:
    remote = root / "remote.git"
    checkout = root / "checkout"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(checkout)], check=True, capture_output=True)
    _run_git(checkout, "switch", "-c", "main")
    (checkout / "README.md").write_text("# Test catalog\n")
    _run_git(checkout, "add", "README.md")
    _run_git(
        checkout,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "commit",
        "-m",
        "Initialize catalog",
    )
    _run_git(checkout, "push", "-u", "origin", "main")
    return remote, checkout


_REPOSITORY = "example/inky-bird-frame"


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", zlib.crc32(chunk_type + data))
    )


def _insert_png_chunk_before_end(path: Path, chunk: bytes) -> None:
    raw = path.read_bytes()
    end = raw.rindex(b"IEND") - 4
    path.write_bytes(raw[:end] + chunk + raw[end:])


def _refresh_asset_checksums(directory: Path) -> None:
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for name in ("portrait", "display"):
        manifest["assets"][name]["sha256"] = sha256_file(directory / f"{name}.png")
    write_json_atomic(manifest_path, manifest)
    rebuild_catalog_index(directory.parent.parent)


def _set_profile_field(directory: Path, key: str, value: str) -> None:
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["profile"][key] = value
    write_json_atomic(manifest_path, manifest)
    write_json_atomic(directory / "profile.json", manifest["profile"])


def _remote_branches(remote: Path) -> list[str]:
    return _run_git(remote, "for-each-ref", "--format=%(refname:short)", "refs/heads/").splitlines()


def _remote_files(remote: Path) -> list[str]:
    return _run_git(
        remote, "-c", "core.quotePath=false", "ls-tree", "-r", "--name-only", "main"
    ).splitlines()


def _result_items(result: dict[str, object], key: str) -> list[dict[str, object]]:
    value = result[key]
    assert isinstance(value, list)
    items = [item for item in value if isinstance(item, dict)]
    assert len(items) == len(value)
    return items


class _FakeGitHub:
    """A `gh` stand-in that keeps pull requests against the local bare remote."""

    def __init__(self, test: unittest.TestCase, remote: Path) -> None:
        self.test = test
        self.remote = remote
        self.calls: list[tuple[str, ...]] = []
        self.pull_requests: list[dict[str, object]] = []
        self.merged_commits: list[str] = []
        self.fail_next_merge = False
        self.fail_next_create = False

    def add_pull_request(self, branch: str, *, author: str = "example") -> dict[str, object]:
        number = len(self.pull_requests) + 1
        pull_request: dict[str, object] = {
            "number": number,
            "url": f"https://github.com/{_REPOSITORY}/pull/{number}",
            "state": "OPEN",
            "headRefName": branch,
            "headRefOid": _run_git(self.remote, "rev-parse", f"refs/heads/{branch}"),
            "isCrossRepository": False,
            "author": {"login": author},
        }
        self.pull_requests.append(pull_request)
        return pull_request

    def _by_url(self, url: str) -> dict[str, object]:
        matches = [item for item in self.pull_requests if item["url"] == url]
        self.test.assertEqual(len(matches), 1, url)
        return matches[0]

    def __call__(
        self,
        _publication: object,
        *arguments: str,
        input_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del input_text, check
        self.calls.append(arguments)
        stdout = ""
        if arguments[:2] == ("api", "user"):
            stdout = "example\n"
        elif arguments[:2] == ("pr", "list"):
            self.test.assertEqual(arguments[arguments.index("--repo") + 1], _REPOSITORY)
            state = arguments[arguments.index("--state") + 1]
            matches = [
                item
                for item in self.pull_requests
                if state == "all" or item["state"] == state.upper()
            ]
            if "--head" in arguments:
                head = arguments[arguments.index("--head") + 1]
                matches = [item for item in matches if item["headRefName"] == head]
            # Like gh, list the newest pull request first.
            stdout = json.dumps(matches[::-1])
        elif arguments[:2] == ("pr", "create"):
            branch = arguments[arguments.index("--head") + 1]
            self.test.assertEqual(arguments[arguments.index("--base") + 1], "main")
            if self.fail_next_create:
                self.fail_next_create = False
                raise CatalogPublishError("gh pr create failed: simulated outage")
            stdout = str(self.add_pull_request(branch)["url"]) + "\n"
        elif arguments[:2] == ("pr", "merge"):
            pull_request = self._by_url(arguments[2])
            for flag in ("--admin", "--squash", "--delete-branch"):
                self.test.assertIn(flag, arguments)
            commit = arguments[arguments.index("--match-head-commit") + 1]
            branch = str(pull_request["headRefName"])
            self.test.assertEqual(
                _run_git(self.remote, "rev-parse", f"refs/heads/{branch}"), commit
            )
            self.test.assertEqual(pull_request["headRefOid"], commit)
            if self.fail_next_merge:
                self.fail_next_merge = False
                raise CatalogPublishError("gh pr merge failed: simulated outage")
            tree = _run_git(self.remote, "rev-parse", f"{commit}^{{tree}}")
            squashed = _run_git(
                self.remote,
                "-c",
                "user.name=Merge",
                "-c",
                "user.email=merge@example.test",
                "commit-tree",
                tree,
                "-p",
                "refs/heads/main",
                "-m",
                "Squash publication",
            )
            _run_git(self.remote, "update-ref", "refs/heads/main", squashed)
            _run_git(self.remote, "update-ref", "-d", f"refs/heads/{branch}")
            pull_request["state"] = "MERGED"
            self.merged_commits.append(commit)
        elif arguments[:2] == ("pr", "view"):
            stdout = str(self._by_url(arguments[2])["state"]) + "\n"
        elif arguments[:2] == ("pr", "close"):
            pull_request = self._by_url(arguments[2])
            self.test.assertIn("--delete-branch", arguments)
            self.test.assertEqual(pull_request["state"], "OPEN")
            pull_request["state"] = "CLOSED"
            _run_git(self.remote, "update-ref", "-d", f"refs/heads/{pull_request['headRefName']}")
        else:
            self.test.fail(f"Unexpected gh call: {arguments}")
        return subprocess.CompletedProcess(["gh", *arguments], 0, stdout, "")


@contextmanager
def _github(fake: _FakeGitHub) -> Iterator[None]:
    """Run the real checkout validation, mapping only the local remote to GitHub."""

    def remote_repository(url: str) -> str | None:
        return _REPOSITORY if Path(url) == fake.remote else None

    with (
        patch("inky_bird_frame.publisher._gh", side_effect=fake),
        patch("inky_bird_frame.publisher._remote_repository", side_effect=remote_repository),
    ):
        yield


class PublisherTests(unittest.TestCase):
    def test_cleans_only_abandoned_publish_directories(self) -> None:
        with TemporaryDirectory() as temporary, TemporaryDirectory() as external:
            work_parent = Path(temporary)
            abandoned = work_parent / "publish-abandoned"
            (abandoned / "checkout").mkdir(parents=True)
            (abandoned / "checkout/file").write_text("scratch")
            preserved_directory = work_parent / "other-work"
            preserved_directory.mkdir()
            preserved_file = work_parent / "publish-file"
            preserved_file.write_text("not a directory")
            publish_symlink = work_parent / "publish-link"
            outside = Path(external)
            publish_symlink.symlink_to(outside, target_is_directory=True)

            removed = _cleanup_abandoned_publish_work_locked(work_parent)

            self.assertEqual(removed, 1)
            self.assertFalse(abandoned.exists())
            self.assertTrue(preserved_directory.is_dir())
            self.assertTrue(preserved_file.is_file())
            self.assertTrue(publish_symlink.is_symlink())
            self.assertTrue(outside.is_dir())

    def test_finds_abandoned_publish_work_without_removing_it(self) -> None:
        with TemporaryDirectory() as temporary:
            work_parent = Path(temporary)
            abandoned = work_parent / "publish-abandoned"
            abandoned.mkdir()

            found = _abandoned_publish_work(work_parent)

            self.assertEqual(found, (abandoned,))
            self.assertTrue(abandoned.is_dir())

    def test_abandoned_publish_cleanup_fails_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            work_parent = Path(temporary)
            abandoned = work_parent / "publish-abandoned"
            abandoned.mkdir()

            with (
                patch(
                    "inky_bird_frame.publisher.shutil.rmtree",
                    side_effect=PermissionError("denied"),
                ),
                self.assertRaisesRegex(
                    CatalogPublishError,
                    "Could not remove abandoned catalog publication work",
                ),
            ):
                _cleanup_abandoned_publish_work_locked(work_parent)

    def test_cleans_abandoned_work_before_checkout_validation(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "checkout"
            checkout.mkdir()
            config = load_config(_write_config(root, checkout))
            abandoned = config.controller.state_dir / "catalog-publish-work/publish-abandoned"
            abandoned.mkdir(parents=True)

            with (
                patch(
                    "inky_bird_frame.publisher._validate_checkout",
                    side_effect=CatalogPublishError("checkout unavailable"),
                ),
                self.assertRaisesRegex(CatalogPublishError, "checkout unavailable"),
            ):
                run_catalog_publish(config)

            self.assertFalse(abandoned.exists())

    def test_birdbuddy_private_fields_are_rejected_from_catalog_json(self) -> None:
        for field in (
            "authorization_confirmed_at",
            "birdbuddy",
            "email",
            "feeder_id",
            "password",
            "postcard_id",
            "refresh_token",
        ):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(CatalogPublishError, "Private field"),
            ):
                _check_json_privacy({field: "private"}, Path("manifest.json"))

    def test_parses_only_supported_github_repository_remotes(self) -> None:
        self.assertEqual(
            _remote_repository("https://github.com/example/inky-bird-frame.git"),
            "example/inky-bird-frame",
        )
        self.assertEqual(
            _remote_repository("git@github.com:example/inky-bird-frame.git"),
            "example/inky-bird-frame",
        )
        self.assertIsNone(_remote_repository("https://example.test/example/inky-bird-frame"))
        self.assertIsNone(
            _remote_repository("https://token@github.com/example/inky-bird-frame.git")
        )

    def test_checkout_requires_github_cli_owner_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            checkout = Path(temporary) / "checkout"
            subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
            _run_git(
                checkout,
                "remote",
                "add",
                "origin",
                "https://github.com/example/inky-bird-frame.git",
            )
            publication = PublicCatalogConfig(
                enabled=True,
                checkout_dir=checkout,
                repository="example/inky-bird-frame",
                gh_path=Path("/usr/bin/false"),
            )
            gh_result = subprocess.CompletedProcess(["gh"], 0, "intruder\n", "")

            with (
                patch("inky_bird_frame.publisher._gh", return_value=gh_result),
                self.assertRaisesRegex(CatalogPublishError, "repository owner 'example'"),
            ):
                _validate_checkout(checkout, publication)

    def test_checkout_reports_github_authentication_failure_after_api_error(self) -> None:
        with TemporaryDirectory() as temporary:
            checkout = Path(temporary) / "checkout"
            subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
            _run_git(
                checkout,
                "remote",
                "add",
                "origin",
                "https://github.com/example/inky-bird-frame.git",
            )
            publication = PublicCatalogConfig(
                enabled=True,
                checkout_dir=checkout,
                repository="example/inky-bird-frame",
                gh_path=Path("/usr/bin/false"),
            )

            with (
                patch(
                    "inky_bird_frame.publisher._gh",
                    side_effect=[
                        CatalogPublishError("gh api failed: invalid response"),
                        subprocess.CompletedProcess(
                            ["gh", "auth"],
                            1,
                            "",
                            "The token in hosts.yml is invalid.",
                        ),
                    ],
                ) as github,
                self.assertRaisesRegex(CatalogPublishError, "token in hosts.yml is invalid"),
            ):
                _validate_checkout(checkout, publication)

            self.assertEqual(
                github.call_args_list,
                [
                    call(publication, "api", "user", "--jq", ".login"),
                    call(
                        publication,
                        "auth",
                        "status",
                        "--hostname",
                        "github.com",
                        check=False,
                    ),
                ],
            )

    @pytest.mark.repository_catalog
    def test_repository_seed_catalog_is_publishable(self) -> None:
        catalog = Path(__file__).parents[1] / "catalog"

        entries = validate_public_catalog(catalog)

        self.assertTrue({7562, 9083, 11935, 12942}.issubset({entry.taxon_id for entry in entries}))

    def test_validates_and_syncs_an_approved_catalog(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            _create_species(source, 1, "Example Bird")

            result = sync_public_catalog(source, destination)

            self.assertEqual(result["already_present"], [])
            self.assertEqual(
                result["published"],
                [
                    {
                        "taxon_id": 1,
                        "common_name": "Example Bird",
                        "scientific_name": "Avis exemplaris",
                        "slug": "example-bird",
                    }
                ],
            )
            self.assertEqual(len(validate_public_catalog(destination)), 1)

            repeated = sync_public_catalog(source, destination)

            self.assertEqual(repeated["published"], [])
            self.assertEqual(repeated["replaced"], [])
            self.assertEqual(repeated["already_present"], [1])

    def test_controller_sync_stages_in_owned_state_with_a_protected_parent(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("A non-root caller must exercise the protected parent")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            protected = root / "protected"
            destination = protected / "catalog"
            source = root / "source"
            state = root / "state"
            original = _create_species(destination, 1, "First Bird")
            _create_species(source, 1, "First Bird")
            _create_species(source, 2, "Second Bird")
            before = {p.name: p.read_bytes() for p in original.iterdir()}
            protected.chmod(0o555)
            try:
                args = build_parser().parse_args(
                    [
                        "catalog",
                        "sync",
                        "--source-catalog",
                        str(source),
                        "--catalog",
                        str(destination),
                        "--state-dir",
                        str(state),
                    ]
                )
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(catalog_sync_command(args), 0)
                self.assertEqual(len(validate_public_catalog(destination)), 2)
                self.assertEqual(before, {p.name: p.read_bytes() for p in original.iterdir()})
                self.assertEqual(stat.S_IMODE(protected.stat().st_mode), 0o555)
                self.assertEqual(list((state / "catalog-sync-work").iterdir()), [])
                self.assertFalse(list(protected.glob(".catalog-sync-*")))
            finally:
                protected.chmod(0o755)

    def test_custom_transaction_directory_rejects_unsafe_locations(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            _create_species(source, 1, "First Bird")
            _create_species(destination, 2, "Second Bird")
            before = (destination / "index.json").read_bytes()
            shared = root / "shared"
            shared.mkdir(mode=0o777)
            shared.chmod(0o777)
            link = root / "link"
            link.symlink_to(shared, target_is_directory=True)
            for directory, message in (
                (link, "symlink"),
                (shared, "caller-owned"),
                (source / "work", "outside both"),
                (destination / "work", "outside both"),
            ):
                with self.subTest(directory=directory):
                    with self.assertRaisesRegex(CatalogPublishError, message):
                        sync_public_catalog(source, destination, transaction_dir=directory)
                    self.assertEqual((destination / "index.json").read_bytes(), before)
            self.assertFalse((source / "work").exists())
            self.assertFalse((destination / "work").exists())

    def test_custom_transaction_directory_rejects_foreign_owner_or_filesystem(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            work = root / "work"
            work.mkdir(mode=0o700)
            _create_species(source, 1, "First Bird")
            _create_species(destination, 2, "Second Bird")
            before = (destination / "index.json").read_bytes()
            with (
                patch("inky_bird_frame.publisher.os.geteuid", return_value=os.geteuid() + 1),
                self.assertRaisesRegex(CatalogPublishError, "caller-owned"),
            ):
                sync_public_catalog(source, destination, transaction_dir=work)
            work = work.resolve()
            real_lstat = Path.lstat

            def other_device(path: Path) -> os.stat_result:
                info = real_lstat(path)
                if path == work:
                    fields = list(info)
                    fields[2] += 1
                    return os.stat_result(fields)
                return info

            with (
                patch.object(Path, "lstat", other_device),
                self.assertRaisesRegex(CatalogPublishError, "destination filesystem"),
            ):
                sync_public_catalog(source, destination, transaction_dir=work)
            self.assertEqual((destination / "index.json").read_bytes(), before)
            self.assertEqual(list(work.iterdir()), [])

    def test_custom_transaction_directory_rejects_replaceable_ancestors(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "source"
            destination = root / "destination"
            parent = root / "shared-parent"
            work = parent / "private-work"
            work.mkdir(mode=0o700, parents=True)
            _create_species(source, 1, "First Bird")
            _create_species(destination, 2, "Second Bird")
            before = (destination / "index.json").read_bytes()
            parent.chmod(0o777)
            with self.assertRaisesRegex(CatalogPublishError, "ancestors"):
                sync_public_catalog(source, destination, transaction_dir=work)
            parent.chmod(0o755)
            real_lstat = Path.lstat

            def foreign_parent(path: Path) -> os.stat_result:
                info = real_lstat(path)
                if path == parent:
                    fields = list(info)
                    fields[4] = os.geteuid() + 1
                    return os.stat_result(fields)
                return info

            with (
                patch.object(Path, "lstat", foreign_parent),
                self.assertRaisesRegex(CatalogPublishError, "ancestors"),
            ):
                sync_public_catalog(source, destination, transaction_dir=work)
            self.assertEqual((destination / "index.json").read_bytes(), before)
            self.assertEqual(list(work.iterdir()), [])

    def test_custom_recovery_rejects_a_shared_writable_transaction(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            work = root / "work"
            work.mkdir(mode=0o700)
            _create_species(source, 1, "First Bird")
            _create_species(destination, 2, "Second Bird")
            before = (destination / "index.json").read_bytes()
            transaction = (
                work / f"{_catalog_transaction_prefix(destination, committed=False)}foreign"
            )
            transaction.mkdir()
            transaction.chmod(0o777)
            with self.assertRaisesRegex(CatalogPublishError, "caller custody"):
                sync_public_catalog(source, destination, transaction_dir=work)
            self.assertEqual((destination / "index.json").read_bytes(), before)
            self.assertTrue(transaction.exists())

    def test_custom_transaction_directory_does_not_hide_legacy_recovery(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            _create_species(source, 1, "First Bird")
            _create_species(destination, 2, "Second Bird")
            before = (destination / "index.json").read_bytes()
            for committed in (False, True):
                with self.subTest(committed=committed):
                    prefix = _catalog_transaction_prefix(destination, committed=committed)
                    legacy = root / f"{prefix}interrupted"
                    legacy.mkdir()
                    try:
                        with self.assertRaisesRegex(
                            CatalogPublishError, "Recover existing sibling"
                        ):
                            sync_public_catalog(source, destination, transaction_dir=root / "work")
                        self.assertTrue(legacy.is_dir())
                        self.assertFalse((root / "work").exists())
                        self.assertEqual((destination / "index.json").read_bytes(), before)
                    finally:
                        legacy.rmdir()

    def test_custom_transaction_directory_recovers_active_replacement(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            replacement = root / "replacement"
            work = root / "state/catalog-sync-work"
            work.parent.mkdir(mode=0o700)
            approved = _create_species(destination, 1, "First Bird")
            original = (approved / "portrait.png").read_bytes()
            shutil.copytree(destination, source)
            _create_portrait_replacement(
                replacement, 1, "First Bird", approved_at="2026-07-10T00:00:00+00:00", color="black"
            )
            replace_public_catalog_taxon(replacement, source, 1, "Reviewed correction.")
            with (
                patch(
                    "inky_bird_frame.publisher._commit_catalog_transaction",
                    side_effect=OSError("interrupted before commit"),
                ),
                self.assertRaisesRegex(OSError, "before commit"),
            ):
                sync_public_catalog(
                    source, destination, allow_replacements=True, transaction_dir=work
                )
            active = list(
                work.glob(f"{_catalog_transaction_prefix(destination, committed=False)}*")
            )
            self.assertEqual(len(active), 1)
            self.assertEqual(
                (active[0] / "approved/1-first-bird/portrait.png").read_bytes(), original
            )
            restored: list[bytes] = []
            from inky_bird_frame.publisher import _restore_catalog_replacements

            def capture_restore(transaction: Path, species: Path) -> None:
                _restore_catalog_replacements(transaction, species)
                restored.append((species / "1-first-bird/portrait.png").read_bytes())

            with patch(
                "inky_bird_frame.publisher._restore_catalog_replacements",
                side_effect=capture_restore,
            ):
                sync_public_catalog(
                    source, destination, allow_replacements=True, transaction_dir=work
                )
            self.assertEqual(restored, [original])
            self.assertEqual(list(work.iterdir()), [])
            self.assertEqual(
                (approved / "portrait.png").read_bytes(),
                (source / "species/1-first-bird/portrait.png").read_bytes(),
            )
            self.assertEqual(len(validate_public_catalog(destination)), 1)

    def test_custom_transaction_directory_recovers_committed_cleanup(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            work = root / "work"
            _create_species(source, 1, "First Bird")
            real_rmtree = shutil.rmtree

            def interrupt_cleanup(path: Path) -> None:
                if Path(path).name.startswith(
                    _catalog_transaction_prefix(destination, committed=True)
                ):
                    raise OSError("interrupted committed cleanup")
                real_rmtree(path)

            with (
                patch("inky_bird_frame.publisher.shutil.rmtree", side_effect=interrupt_cleanup),
                self.assertRaisesRegex(OSError, "committed cleanup"),
            ):
                sync_public_catalog(source, destination, transaction_dir=work)
            prefix = _catalog_transaction_prefix(destination, committed=True)
            self.assertEqual(len(list(work.glob(f"{prefix}*"))), 1)
            result = sync_public_catalog(source, destination, transaction_dir=work)
            self.assertEqual(result["already_present"], [1])
            self.assertEqual(list(work.iterdir()), [])
            self.assertEqual(len(validate_public_catalog(destination)), 1)

    def test_sync_applies_a_hash_bound_catalog_replacement(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "destination"
            source = root / "source"
            replacement = root / "replacement"
            _create_species(destination, 1, "Example Bird")
            shutil.copytree(destination, source)
            replacement_directory = _create_species(replacement, 1, "Example Bird")
            portrait = replacement_directory / "portrait.png"
            Image.new("RGB", (1200, 1600), "black").save(portrait)
            manifest_path = replacement_directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["approved_at"] = "2026-07-10T00:00:00+00:00"
            manifest["assets"]["portrait"]["sha256"] = sha256_file(portrait)
            write_json_atomic(manifest_path, manifest)
            rebuild_catalog_index(replacement)
            replace_public_catalog_taxon(
                replacement,
                source,
                1,
                "Human review found incorrect proportions.",
            )

            result = sync_public_catalog(source, destination, allow_replacements=True)

            self.assertEqual(result["published"], [])
            self.assertEqual(
                result["replaced"],
                [
                    {
                        "taxon_id": 1,
                        "common_name": "Example Bird",
                        "scientific_name": "Avis exemplaris",
                        "slug": "example-bird",
                    }
                ],
            )
            self.assertEqual(result["already_present"], [])
            self.assertEqual(
                (destination / "species/1-example-bird/portrait.png").read_bytes(),
                (source / "species/1-example-bird/portrait.png").read_bytes(),
            )
            self.assertEqual(len(validate_public_catalog(destination)), 1)

    def test_sync_converges_migration_metadata_on_an_already_corrected_approval(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            published = root / "published"
            corrected_local = root / "corrected-local"
            replacement = root / "replacement"
            _create_species(published, 1, "Example Bird")
            replacement_directory = _create_portrait_replacement(
                replacement,
                1,
                "Example Bird",
                approved_at="2026-07-10T00:00:00+00:00",
                color="black",
            )
            shutil.copytree(replacement, corrected_local)
            replace_public_catalog_taxon(
                replacement,
                published,
                1,
                "Human review found incorrect proportions.",
            )
            self.assertNotIn(
                "catalog_migration",
                json.loads((replacement_directory / "manifest.json").read_text()),
            )

            result = sync_public_catalog(
                published,
                corrected_local,
                allow_replacements=True,
            )

            replaced = result["replaced"]
            self.assertIsInstance(replaced, list)
            assert isinstance(replaced, list)
            self.assertEqual([item["taxon_id"] for item in replaced], [1])
            self.assertEqual(
                (corrected_local / "species/1-example-bird/manifest.json").read_bytes(),
                (published / "species/1-example-bird/manifest.json").read_bytes(),
            )
            self.assertEqual(len(validate_public_catalog(corrected_local)), 1)

    def test_sync_retains_a_newer_reviewed_destination(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            replacement = root / "replacement"
            _create_species(source, 1, "Example Bird")
            shutil.copytree(source, destination)
            replacement_directory = _create_portrait_replacement(
                replacement,
                1,
                "Example Bird",
                approved_at="2026-07-10T00:00:00+00:00",
                color="black",
            )
            replace_public_catalog_taxon(
                replacement,
                destination,
                1,
                "Human review found incorrect proportions.",
            )
            newer_portrait = (replacement_directory / "portrait.png").read_bytes()

            result = sync_public_catalog(source, destination, allow_replacements=True)

            self.assertEqual(result["published"], [])
            self.assertEqual(result["replaced"], [])
            self.assertEqual(
                result["retained_newer"],
                [
                    {
                        "taxon_id": 1,
                        "common_name": "Example Bird",
                        "scientific_name": "Avis exemplaris",
                        "slug": "example-bird",
                    }
                ],
            )
            self.assertEqual(
                (destination / "species/1-example-bird/portrait.png").read_bytes(),
                newer_portrait,
            )
            self.assertEqual(len(validate_public_catalog(destination)), 1)

    def test_sync_retains_newer_migration_metadata_for_matching_assets(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original"
            published = root / "published"
            corrected_without_migration = root / "corrected-without-migration"
            replacement = root / "replacement"
            _create_species(original, 1, "Example Bird")
            shutil.copytree(original, published)
            _create_portrait_replacement(
                replacement,
                1,
                "Example Bird",
                approved_at="2026-07-10T00:00:00+00:00",
                color="black",
            )
            shutil.copytree(replacement, corrected_without_migration)
            replace_public_catalog_taxon(
                replacement,
                published,
                1,
                "Human review found incorrect proportions.",
            )

            result = sync_public_catalog(
                corrected_without_migration,
                published,
                allow_replacements=True,
            )

            self.assertEqual(result["replaced"], [])
            retained_newer = result["retained_newer"]
            self.assertIsInstance(retained_newer, list)
            assert isinstance(retained_newer, list)
            self.assertEqual([item["taxon_id"] for item in retained_newer], [1])
            manifest = json.loads((published / "species/1-example-bird/manifest.json").read_text())
            self.assertEqual(
                manifest["catalog_migration"]["reason"],
                "Human review found incorrect proportions.",
            )

    def test_sync_rejects_a_valid_replacement_without_explicit_opt_in(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "destination"
            source = root / "source"
            replacement = root / "replacement"
            original = _create_species(destination, 1, "Example Bird")
            original_portrait = (original / "portrait.png").read_bytes()
            shutil.copytree(destination, source)
            _create_portrait_replacement(
                replacement,
                1,
                "Example Bird",
                approved_at="2026-07-10T00:00:00+00:00",
                color="black",
            )
            replace_public_catalog_taxon(
                replacement,
                source,
                1,
                "Human review found incorrect proportions.",
            )

            with self.assertRaisesRegex(CatalogPublishError, "immutable local approval"):
                sync_public_catalog(source, destination)

            self.assertEqual(
                (destination / "species/1-example-bird/portrait.png").read_bytes(),
                original_portrait,
            )

    def test_bootstrap_retains_an_independent_local_approval_and_adds_new_taxa(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            _create_species(source, 1, "Example Bird")
            _create_species(source, 2, "Second Bird")
            local = _create_portrait_replacement(
                destination,
                1,
                "Example Bird",
                approved_at="2026-07-10T00:00:00+00:00",
                color="black",
            )
            local_portrait = (local / "portrait.png").read_bytes()

            with self.assertRaisesRegex(CatalogPublishError, "changed immutable taxon 1"):
                sync_public_catalog(source, destination, allow_replacements=True)

            result = sync_public_catalog(
                source,
                destination,
                retain_independent_approvals=True,
            )

            self.assertEqual(
                result["retained_independent"],
                [
                    {
                        "taxon_id": 1,
                        "common_name": "Example Bird",
                        "scientific_name": "Avis exemplaris",
                        "slug": "example-bird",
                    }
                ],
            )
            published = result["published"]
            self.assertIsInstance(published, list)
            assert isinstance(published, list)
            self.assertEqual([item["taxon_id"] for item in published], [2])
            self.assertEqual(
                (destination / "species/1-example-bird/portrait.png").read_bytes(),
                local_portrait,
            )
            self.assertEqual(
                [entry.taxon_id for entry in validate_public_catalog(destination)],
                [1, 2],
            )

    def test_repeated_replacements_preserve_ancestry_for_a_skipped_upgrade(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original"
            first = root / "first"
            first_source = root / "first-source"
            second = root / "second"
            second_source = root / "second-source"
            skipped_controller = root / "skipped-controller"
            _create_species(original, 1, "Example Bird")
            shutil.copytree(original, first)
            _create_portrait_replacement(
                first_source,
                1,
                "Example Bird",
                approved_at="2026-07-10T00:00:00+00:00",
                color="black",
            )
            replace_public_catalog_taxon(
                first_source,
                first,
                1,
                "First reviewed correction.",
            )
            shutil.copytree(first, second)
            _create_portrait_replacement(
                second_source,
                1,
                "Example Bird",
                approved_at="2026-07-11T00:00:00+00:00",
                color="gray",
            )
            replace_public_catalog_taxon(
                second_source,
                second,
                1,
                "Second reviewed correction.",
            )

            second_manifest = json.loads(
                (second / "species/1-example-bird/manifest.json").read_text()
            )
            migration = second_manifest["catalog_migration"]
            self.assertEqual(migration["reason"], "Second reviewed correction.")
            self.assertEqual(
                [record["reason"] for record in migration["history"]],
                ["First reviewed correction."],
            )
            self.assertEqual(validate_catalog_additions(first, second), [])
            tampered = root / "tampered"
            shutil.copytree(second, tampered)
            tampered_manifest_path = tampered / "species/1-example-bird/manifest.json"
            tampered_manifest = json.loads(tampered_manifest_path.read_text())
            tampered_manifest["catalog_migration"].pop("history")
            write_json_atomic(tampered_manifest_path, tampered_manifest)
            rebuild_catalog_index(tampered)
            with self.assertRaisesRegex(CatalogPublishError, "preserve migration history"):
                validate_catalog_additions(first, tampered)

            forked = root / "forked"
            shutil.copytree(second, forked)
            forked_manifest_path = forked / "species/1-example-bird/manifest.json"
            forked_manifest = json.loads(forked_manifest_path.read_text())
            forked_migration = forked_manifest["catalog_migration"]
            forked_migration["history"].append(
                {
                    "reason": "Unrelated fork.",
                    "replaces": {
                        "approved_at": "2026-07-09T12:00:00+00:00",
                        "display_sha256": "c" * 64,
                        "portrait_sha256": "d" * 64,
                    },
                }
            )
            write_json_atomic(forked_manifest_path, forked_manifest)
            rebuild_catalog_index(forked)
            self.assertEqual(len(validate_public_catalog(forked)), 1)

            forked_destination = root / "forked-destination"
            shutil.copytree(first, forked_destination)
            with self.assertRaisesRegex(
                CatalogPublishError,
                "does not match the approved base artifacts",
            ):
                sync_public_catalog(forked, forked_destination, allow_replacements=True)
            with self.assertRaisesRegex(
                CatalogPublishError,
                "preserve migration history",
            ):
                sync_public_catalog(first, forked, allow_replacements=True)

            shutil.copytree(original, skipped_controller)
            result = sync_public_catalog(
                second,
                skipped_controller,
                allow_replacements=True,
            )

            replaced = result["replaced"]
            self.assertIsInstance(replaced, list)
            assert isinstance(replaced, list)
            self.assertEqual([item["taxon_id"] for item in replaced], [1])
            self.assertEqual(
                (skipped_controller / "species/1-example-bird/portrait.png").read_bytes(),
                (second / "species/1-example-bird/portrait.png").read_bytes(),
            )
            self.assertEqual(len(validate_public_catalog(skipped_controller)), 1)

            retained = sync_public_catalog(original, second, allow_replacements=True)

            retained_newer = retained["retained_newer"]
            self.assertIsInstance(retained_newer, list)
            assert isinstance(retained_newer, list)
            self.assertEqual([item["taxon_id"] for item in retained_newer], [1])
            self.assertEqual(
                (second / "species/1-example-bird/portrait.png").read_bytes(),
                (second_source / "species/1-example-bird/portrait.png").read_bytes(),
            )

    def test_interrupted_committed_cleanup_keeps_the_installed_replacement(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "destination"
            source = root / "source"
            replacement = root / "replacement"
            _create_species(destination, 1, "Example Bird")
            shutil.copytree(destination, source)
            _create_portrait_replacement(
                replacement,
                1,
                "Example Bird",
                approved_at="2026-07-10T00:00:00+00:00",
                color="black",
            )
            replace_public_catalog_taxon(
                replacement,
                source,
                1,
                "Human review found incorrect proportions.",
            )
            real_rmtree = shutil.rmtree

            def interrupt_committed_cleanup(path: Path) -> None:
                transaction = Path(path)
                committed_prefix = _catalog_transaction_prefix(destination, committed=True)
                if transaction.name.startswith(committed_prefix):
                    backup = transaction / "approved/1-example-bird"
                    real_rmtree(backup)
                    raise OSError("simulated interrupted committed cleanup")
                real_rmtree(transaction)

            with (
                patch(
                    "inky_bird_frame.publisher.shutil.rmtree",
                    side_effect=interrupt_committed_cleanup,
                ),
                self.assertRaisesRegex(OSError, "interrupted committed cleanup"),
            ):
                sync_public_catalog(source, destination, allow_replacements=True)

            committed_prefix = _catalog_transaction_prefix(destination, committed=True)
            self.assertEqual(len(list(root.glob(f"{committed_prefix}*"))), 1)
            self.assertEqual(
                (destination / "species/1-example-bird/portrait.png").read_bytes(),
                (source / "species/1-example-bird/portrait.png").read_bytes(),
            )

            recovered = sync_public_catalog(source, destination, allow_replacements=True)

            self.assertEqual(recovered["already_present"], [1])
            self.assertFalse(list(root.glob(f"{committed_prefix}*")))
            self.assertEqual(len(validate_public_catalog(destination)), 1)

    def test_sync_rolls_back_a_replacement_when_a_later_taxon_conflicts(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "destination"
            source = root / "source"
            replacement = root / "replacement"
            original = _create_species(destination, 1, "First Bird")
            _create_species(destination, 2, "Second Bird")
            original_portrait = (original / "portrait.png").read_bytes()
            shutil.copytree(destination, source)
            replacement_directory = _create_species(replacement, 1, "First Bird")
            portrait = replacement_directory / "portrait.png"
            Image.new("RGB", (1200, 1600), "black").save(portrait)
            manifest_path = replacement_directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["approved_at"] = "2026-07-10T00:00:00+00:00"
            manifest["assets"]["portrait"]["sha256"] = sha256_file(portrait)
            write_json_atomic(manifest_path, manifest)
            rebuild_catalog_index(replacement)
            replace_public_catalog_taxon(
                replacement,
                source,
                1,
                "Human review found incorrect proportions.",
            )
            conflicting_portrait = source / "species/2-second-bird/portrait.png"
            Image.new("RGB", (1200, 1600), "gray").save(conflicting_portrait)
            conflicting_manifest_path = source / "species/2-second-bird/manifest.json"
            conflicting_manifest = json.loads(conflicting_manifest_path.read_text())
            conflicting_manifest["assets"]["portrait"]["sha256"] = sha256_file(conflicting_portrait)
            write_json_atomic(conflicting_manifest_path, conflicting_manifest)
            rebuild_catalog_index(source)

            with self.assertRaisesRegex(CatalogPublishError, "changed immutable taxon 2"):
                sync_public_catalog(source, destination, allow_replacements=True)

            self.assertEqual(
                (destination / "species/1-first-bird/portrait.png").read_bytes(),
                original_portrait,
            )
            active_prefix = _catalog_transaction_prefix(destination, committed=False)
            self.assertEqual(len(list(root.glob(f"{active_prefix}*"))), 1)

    def test_syncs_only_requested_taxa_for_a_contribution(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            _create_species(source, 1, "First Bird")
            _create_species(source, 2, "Second Bird")

            result = sync_public_catalog(source, destination, taxon_ids={2})

            self.assertEqual(
                result["published"],
                [
                    {
                        "taxon_id": 2,
                        "common_name": "Second Bird",
                        "scientific_name": "Avis exemplaris",
                        "slug": "second-bird",
                    }
                ],
            )
            self.assertEqual(
                [entry.taxon_id for entry in validate_public_catalog(destination)],
                [2],
            )

    def test_sync_recovers_complete_species_when_initial_index_was_interrupted(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            _create_species(source, 1, "Example Bird")
            (destination / "species").mkdir(parents=True)
            shutil.copytree(
                source / "species/1-example-bird",
                destination / "species/1-example-bird",
            )

            result = sync_public_catalog(source, destination)

            self.assertEqual(result["published"], [])
            self.assertEqual(result["already_present"], [1])
            self.assertEqual(len(validate_public_catalog(destination)), 1)

    def test_sync_removes_interrupted_staging_directory_before_retry(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            _create_species(source, 1, "Example Bird")
            active_prefix = _catalog_transaction_prefix(destination, committed=False)
            staging = root / f"{active_prefix}interrupted/1-example-bird"
            staging.mkdir(parents=True)
            (staging / "manifest.json").write_text("partial")

            result = sync_public_catalog(source, destination)

            self.assertFalse(staging.parent.exists())
            self.assertEqual(
                result["published"],
                [
                    {
                        "taxon_id": 1,
                        "common_name": "Example Bird",
                        "scientific_name": "Avis exemplaris",
                        "slug": "example-bird",
                    }
                ],
            )
            self.assertEqual(len(validate_public_catalog(destination)), 1)

    def test_sync_recovers_stale_index_only_with_interrupted_transaction_marker(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            _create_species(source, 1, "First Bird")
            _create_species(source, 2, "Second Bird")
            _create_species(destination, 1, "First Bird")
            shutil.copytree(
                source / "species/2-second-bird",
                destination / "species/2-second-bird",
            )
            active_prefix = _catalog_transaction_prefix(destination, committed=False)
            (root / f"{active_prefix}interrupted").mkdir()

            result = sync_public_catalog(source, destination)

            self.assertEqual(result["published"], [])
            self.assertEqual(result["already_present"], [1, 2])
            self.assertEqual(len(validate_public_catalog(destination)), 2)

    def test_sync_recovery_ignores_finder_metadata(self) -> None:
        for interrupted_index in (True, False):
            with (
                self.subTest(interrupted_index=interrupted_index),
                TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                source = root / "source"
                destination = root / "destination"
                _create_species(source, 1, "First Bird")
                _create_species(source, 2, "Second Bird")
                if interrupted_index:
                    _create_species(destination, 1, "First Bird")
                    active_prefix = _catalog_transaction_prefix(destination, committed=False)
                    (root / f"{active_prefix}interrupted").mkdir()
                else:
                    (destination / "species").mkdir(parents=True)
                    shutil.copytree(
                        source / "species/1-first-bird", destination / "species/1-first-bird"
                    )
                shutil.copytree(
                    source / "species/2-second-bird", destination / "species/2-second-bird"
                )
                (destination / "species/.DS_Store").write_bytes(b"finder")

                result = sync_public_catalog(source, destination)

                self.assertEqual(result["already_present"], [1, 2])
                self.assertEqual(len(validate_public_catalog(destination)), 2)

    def test_replacement_recovery_ignores_finder_metadata_in_the_backup(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "destination"
            replacement = root / "replacement"
            approved = _create_species(destination, 1, "Existing Bird")
            _create_portrait_replacement(
                replacement,
                1,
                "Existing Bird",
                approved_at="2026-07-10T00:00:00+00:00",
                color="black",
            )
            active_prefix = _catalog_transaction_prefix(destination, committed=False)
            backup = root / f"{active_prefix}interrupted/approved/1-existing-bird"
            backup.parent.mkdir(parents=True)
            (backup.parent / ".DS_Store").write_bytes(b"finder")
            approved.replace(backup)

            replace_public_catalog_taxon(
                replacement, destination, 1, "Human review found incorrect proportions."
            )

            self.assertFalse(list(root.glob(f"{active_prefix}*")))
            self.assertEqual(len(validate_public_catalog(destination)), 1)

    def test_sync_recovery_keeps_journal_until_index_rebuild_succeeds(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            replacement = root / "replacement"
            approved = _create_species(destination, 1, "Example Bird")
            original_portrait = (approved / "portrait.png").read_bytes()
            shutil.copytree(destination, source)
            replacement_directory = _create_portrait_replacement(
                replacement,
                1,
                "Example Bird",
                approved_at="2026-07-10T00:00:00+00:00",
                color="black",
            )
            active_prefix = _catalog_transaction_prefix(destination, committed=False)
            transaction = root / f"{active_prefix}interrupted"
            backup = transaction / "approved/1-example-bird"
            backup.parent.mkdir(parents=True)
            approved.replace(backup)
            shutil.copytree(
                replacement_directory,
                destination / "species/1-example-bird",
            )

            with (
                patch(
                    "inky_bird_frame.publisher.rebuild_catalog_index",
                    side_effect=OSError("simulated interrupted index rebuild"),
                ),
                self.assertRaisesRegex(OSError, "interrupted index rebuild"),
            ):
                sync_public_catalog(source, destination, allow_replacements=True)

            self.assertTrue(transaction.exists())
            self.assertEqual(
                (destination / "species/1-example-bird/portrait.png").read_bytes(),
                original_portrait,
            )

            recovered = sync_public_catalog(source, destination, allow_replacements=True)

            self.assertEqual(recovered["already_present"], [1])
            self.assertFalse(transaction.exists())
            self.assertEqual(len(validate_public_catalog(destination)), 1)

    def test_sync_recovery_discards_a_partial_destination_before_rollback(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            approved = _create_species(destination, 1, "Example Bird")
            shutil.copytree(destination, source)
            active_prefix = _catalog_transaction_prefix(destination, committed=False)
            transaction = root / f"{active_prefix}interrupted"
            backup = transaction / "approved/1-example-bird"
            backup.parent.mkdir(parents=True)
            approved.replace(backup)
            partial = destination / "species/1-example-bird"
            partial.mkdir()
            (partial / "manifest.json").write_text("{}")

            recovered = sync_public_catalog(source, destination)

            self.assertEqual(recovered["already_present"], [1])
            self.assertFalse(transaction.exists())
            self.assertEqual(len(validate_public_catalog(destination)), 1)

    def test_sync_preserves_transaction_marker_after_partial_failure(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            recovery_source = root / "recovery-source"
            destination = root / "destination"
            _create_species(source, 1, "First Bird")
            _create_species(source, 2, "Second Bird")
            _create_species(recovery_source, 1, "First Bird")
            _create_species(destination, 2, "Conflicting Bird")

            with self.assertRaisesRegex(CatalogPublishError, "conflicts"):
                sync_public_catalog(source, destination)

            active_prefix = _catalog_transaction_prefix(destination, committed=False)
            self.assertEqual(len(list(root.glob(f"{active_prefix}*"))), 1)
            recovered = sync_public_catalog(recovery_source, destination)

            self.assertEqual(recovered["already_present"], [1])
            self.assertFalse(list(root.glob(f"{active_prefix}*")))
            self.assertEqual(len(validate_public_catalog(destination)), 2)

    def test_rejects_a_requested_taxon_missing_from_the_source_catalog(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            _create_species(source, 1, "Example Bird")

            with self.assertRaisesRegex(CatalogPublishError, "does not contain taxon 2"):
                sync_public_catalog(source, destination, taxon_ids={2})
            self.assertFalse(destination.exists())

    def test_validates_add_only_catalog_contributions(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base"
            candidate = root / "candidate"
            _create_species(base, 1, "Existing Bird")
            shutil.copytree(base, candidate)
            _create_species(candidate, 2, "New Bird")

            additions = validate_catalog_additions(base, candidate)

            self.assertEqual([entry.taxon_id for entry in additions], [2])

    def test_rejects_changes_to_base_catalog_taxa(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base"
            candidate = root / "candidate"
            _create_species(base, 1, "Existing Bird")
            shutil.copytree(base, candidate)
            portrait = candidate / "species/1-existing-bird/portrait.png"
            Image.new("RGB", (1200, 1600), "black").save(portrait)
            manifest_path = candidate / "species/1-existing-bird/manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["assets"]["portrait"]["sha256"] = sha256_file(portrait)
            write_json_atomic(manifest_path, manifest)
            rebuild_catalog_index(candidate)

            with self.assertRaisesRegex(CatalogPublishError, "changed immutable taxon 1"):
                validate_catalog_additions(base, candidate)

    def test_validates_explicit_hash_bound_catalog_replacement(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base"
            candidate = root / "candidate"
            source = root / "source"
            original = _create_species(base, 1, "Existing Bird")
            shutil.copytree(base, candidate)
            replacement = _create_species(source, 1, "Existing Bird")
            Image.new("RGB", (1200, 1600), "black").save(replacement / "portrait.png")
            manifest_path = replacement / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["approved_at"] = "2026-07-10T00:00:00+00:00"
            manifest["assets"]["portrait"]["sha256"] = sha256_file(replacement / "portrait.png")
            write_json_atomic(manifest_path, manifest)
            rebuild_catalog_index(source)

            result = replace_public_catalog_taxon(
                source,
                candidate,
                1,
                "Human review found incorrect proportions.",
            )

            replaced = result["replaced"]
            self.assertIsInstance(replaced, dict)
            assert isinstance(replaced, dict)
            self.assertEqual(replaced["taxon_id"], 1)
            self.assertEqual(validate_catalog_additions(base, candidate), [])
            replaced_manifest = json.loads(
                (candidate / "species/1-existing-bird/manifest.json").read_text()
            )
            self.assertEqual(
                replaced_manifest["catalog_migration"],
                {
                    "reason": "Human review found incorrect proportions.",
                    "replaces": {
                        "approved_at": "2026-07-09T00:00:00+00:00",
                        "display_sha256": sha256_file(original / "display.png"),
                        "portrait_sha256": sha256_file(original / "portrait.png"),
                    },
                },
            )

    def test_catalog_replacement_recovers_an_interrupted_approval_move(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "destination"
            replacement = root / "replacement"
            approved = _create_species(destination, 1, "Existing Bird")
            _create_portrait_replacement(
                replacement,
                1,
                "Existing Bird",
                approved_at="2026-07-10T00:00:00+00:00",
                color="black",
            )
            active_prefix = _catalog_transaction_prefix(destination, committed=False)
            interrupted = root / f"{active_prefix}interrupted"
            backup = interrupted / "approved/1-existing-bird"
            backup.parent.mkdir(parents=True)
            approved.replace(backup)

            result = replace_public_catalog_taxon(
                replacement,
                destination,
                1,
                "Human review found incorrect proportions.",
            )

            replaced = result["replaced"]
            self.assertIsInstance(replaced, dict)
            assert isinstance(replaced, dict)
            self.assertEqual(replaced["taxon_id"], 1)
            self.assertFalse(list(root.glob(f"{active_prefix}*")))
            self.assertEqual(
                (destination / "species/1-existing-bird/portrait.png").read_bytes(),
                (replacement / "species/1-existing-bird/portrait.png").read_bytes(),
            )
            self.assertEqual(len(validate_public_catalog(destination)), 1)

    def test_rejects_catalog_replacement_with_wrong_base_hash(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base"
            candidate = root / "candidate"
            _create_species(base, 1, "Existing Bird")
            shutil.copytree(base, candidate)
            portrait = candidate / "species/1-existing-bird/portrait.png"
            Image.new("RGB", (1200, 1600), "black").save(portrait)
            manifest_path = candidate / "species/1-existing-bird/manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["approved_at"] = "2026-07-10T00:00:00+00:00"
            manifest["assets"]["portrait"]["sha256"] = sha256_file(portrait)
            manifest["catalog_migration"] = {
                "reason": "Human review found incorrect proportions.",
                "replaces": {
                    "approved_at": "2026-07-09T00:00:00+00:00",
                    "display_sha256": "0" * 64,
                    "portrait_sha256": "0" * 64,
                },
            }
            write_json_atomic(manifest_path, manifest)
            rebuild_catalog_index(candidate)

            with self.assertRaisesRegex(CatalogPublishError, "does not match"):
                validate_catalog_additions(base, candidate)

    def test_rejects_removing_a_base_catalog_taxon(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base"
            candidate = root / "candidate"
            _create_species(base, 1, "Existing Bird")
            _create_species(candidate, 2, "Different Bird")

            with self.assertRaisesRegex(CatalogPublishError, "removed immutable taxon 1"):
                validate_catalog_additions(base, candidate)

    def test_rejects_private_manifest_fields(self) -> None:
        for field in (
            "zip_code",
            "postal_code",
            "country_code",
            "geocoder",
            "geocoder_attribution",
            "geoapify_api_key",
            "geoapify_api_key_env",
            "birdnet_go_url",
            "birdnet_analyzer",
            "ebird_archive",
            "submission_id",
            "history_started_at",
            "last_imported_at",
            "observed_on",
            "apiKey",
            "api_key",
            "postalCode",
            "countryCode",
            "geocoderAttribution",
            "geoapifyApiKey",
            "birdnetGoUrl",
            "birdnetAnalyzer",
            "ebirdArchive",
            "submissionId",
            "historyStartedAt",
            "lastImportedAt",
            "observedOn",
        ):
            with self.subTest(field=field), TemporaryDirectory() as temporary:
                catalog = Path(temporary) / "catalog"
                directory = _create_species(catalog, 1, "Example Bird")
                manifest_path = directory / "manifest.json"
                manifest = json.loads(manifest_path.read_text())
                manifest[field] = "private"
                write_json_atomic(manifest_path, manifest)

                with self.assertRaisesRegex(CatalogPublishError, "Private field"):
                    validate_public_catalog(catalog)

    def test_rejects_unexpected_catalog_root_files(self) -> None:
        with TemporaryDirectory() as temporary:
            catalog = Path(temporary) / "catalog"
            _create_species(catalog, 1, "Example Bird")
            (catalog / "notes.txt").write_text("not part of the catalog contract")

            with self.assertRaisesRegex(CatalogPublishError, "Unexpected catalog root entries"):
                validate_public_catalog(catalog)

    def test_rejects_windows_unc_paths(self) -> None:
        with TemporaryDirectory() as temporary:
            catalog = Path(temporary) / "catalog"
            directory = _create_species(catalog, 1, "Example Bird")
            manifest_path = directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["source_path"] = r"\\server\share\bird.png"
            write_json_atomic(manifest_path, manifest)

            with self.assertRaisesRegex(CatalogPublishError, "Local path"):
                validate_public_catalog(catalog)

    def test_rejects_duplicate_taxon_ids(self) -> None:
        with TemporaryDirectory() as temporary:
            catalog = Path(temporary) / "catalog"
            _create_species(catalog, 1, "Example Bird")
            _create_species(catalog, 1, "Second Bird")

            with self.assertRaisesRegex(CatalogPublishError, "duplicate taxon ID 1"):
                validate_public_catalog(catalog)

    def test_rejects_symlinks_in_catalog_entries(self) -> None:
        with TemporaryDirectory() as temporary:
            catalog = Path(temporary) / "catalog"
            directory = _create_species(catalog, 1, "Example Bird")
            profile = directory / "profile.json"
            profile.unlink()
            profile.symlink_to(directory / "manifest.json")

            with self.assertRaisesRegex(CatalogPublishError, "regular files"):
                validate_public_catalog(catalog)

    def test_rejects_symlinked_destination_root_before_writing(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            target = root / "target"
            target.mkdir()
            destination = root / "destination"
            destination.symlink_to(target, target_is_directory=True)
            _create_species(source, 1, "Example Bird")

            with self.assertRaisesRegex(CatalogPublishError, "Catalog root"):
                sync_public_catalog(source, destination)

    def test_rejects_image_metadata(self) -> None:
        with TemporaryDirectory() as temporary:
            catalog = Path(temporary) / "catalog"
            directory = _create_species(catalog, 1, "Example Bird")
            portrait = directory / "portrait.png"
            metadata = PngImagePlugin.PngInfo()
            metadata.add_text("location", "private")
            Image.new("RGB", (1200, 1600), "white").save(portrait, pnginfo=metadata)
            manifest_path = directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["assets"]["portrait"]["sha256"] = sha256_file(portrait)
            write_json_atomic(manifest_path, manifest)

            with self.assertRaisesRegex(CatalogPublishError, "metadata is not allowed"):
                validate_public_catalog(catalog)

    def test_rejects_asset_checksum_mismatch(self) -> None:
        with TemporaryDirectory() as temporary:
            catalog = Path(temporary) / "catalog"
            directory = _create_species(catalog, 1, "Example Bird")
            manifest_path = directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["assets"]["portrait"]["sha256"] = "0" * 64
            write_json_atomic(manifest_path, manifest)

            with self.assertRaisesRegex(CatalogPublishError, "checksum does not match"):
                validate_public_catalog(catalog)

    def test_rejects_index_that_does_not_match_manifests(self) -> None:
        with TemporaryDirectory() as temporary:
            catalog = Path(temporary) / "catalog"
            _create_species(catalog, 1, "Example Bird")
            index_path = catalog / "index.json"
            index = json.loads(index_path.read_text())
            index["species"] = []
            write_json_atomic(index_path, index)

            with self.assertRaisesRegex(CatalogPublishError, "index does not match"):
                validate_public_catalog(catalog)

    def test_rejects_an_automated_review_below_threshold(self) -> None:
        with TemporaryDirectory() as temporary:
            catalog = Path(temporary) / "catalog"
            directory = _create_species(catalog, 1, "Example Bird")
            manifest_path = directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["quality_review"]["species_accuracy"] = 3
            write_json_atomic(manifest_path, manifest)

            with self.assertRaisesRegex(CatalogPublishError, "lacks a publishable quality review"):
                validate_public_catalog(catalog)

    def test_rejects_passing_review_with_actionable_corrections(self) -> None:
        with TemporaryDirectory() as temporary:
            catalog = Path(temporary) / "catalog"
            directory = _create_species(catalog, 1, "Example Bird")
            manifest_path = directory / "manifest.json"
            review_path = directory / "quality-review.json"
            manifest = json.loads(manifest_path.read_text())
            review = manifest["quality_review"]
            review["correction_findings"] = ["Correct the ruler"]
            write_json_atomic(review_path, review)
            write_json_atomic(manifest_path, manifest)

            with self.assertRaisesRegex(CatalogPublishError, "lacks a publishable quality review"):
                validate_public_catalog(catalog)

    def test_rejects_invalid_correction_source_checksum(self) -> None:
        with TemporaryDirectory() as temporary:
            catalog = Path(temporary) / "catalog"
            directory = _create_species(catalog, 1, "Example Bird")
            manifest_path = directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["generation"]["correction_source_sha256"] = "invalid"
            write_json_atomic(manifest_path, manifest)

            with self.assertRaisesRegex(CatalogPublishError, "lacks a publishable quality review"):
                validate_public_catalog(catalog)

    def test_rejects_manifest_asset_path_traversal_before_reading_it(self) -> None:
        with TemporaryDirectory() as temporary:
            catalog = Path(temporary) / "catalog"
            directory = _create_species(catalog, 1, "Example Bird")
            manifest_path = directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["assets"]["portrait"]["filename"] = "../../private.png"
            write_json_atomic(manifest_path, manifest)

            with self.assertRaisesRegex(CatalogPublishError, "must use portrait.png"):
                validate_public_catalog(catalog)

    def test_rejects_changes_to_an_existing_public_taxon(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            _create_species(source, 1, "Example Bird")
            sync_public_catalog(source, destination)
            public_portrait = destination / "species/1-example-bird/portrait.png"
            public_portrait.write_bytes(b"changed")

            with self.assertRaisesRegex(CatalogPublishError, "checksum does not match"):
                sync_public_catalog(source, destination)

    def test_publishes_through_a_real_git_remote_and_supports_dry_run(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, checkout = _initialize_remote(root)
            config = load_config(_write_config(root, checkout))
            _create_species(config.controller.catalog_dir, 1, "Example Bird")
            (config.controller.catalog_dir / ".staging").mkdir()
            abandoned_work = config.controller.state_dir / "catalog-publish-work/publish-abandoned"
            abandoned_work.mkdir(parents=True)
            (abandoned_work / "partial").write_text("scratch")
            fake = _FakeGitHub(self, remote)
            timeouts: dict[str, float] = {}

            def recording_run(
                arguments: list[str],
                *,
                input_text: str | None = None,
                check: bool = True,
                timeout: float,
            ) -> subprocess.CompletedProcess[str]:
                if arguments[0] == "git":
                    subcommand = next(
                        item
                        for index, item in enumerate(arguments[1:], start=1)
                        if not item.startswith("-") and arguments[index - 1] not in {"-C", "-c"}
                    )
                    timeouts[subcommand] = max(timeout, timeouts.get(subcommand, 0))
                return _run(arguments, input_text=input_text, check=check, timeout=timeout)

            with (
                _github(fake),
                patch("inky_bird_frame.publisher._run", side_effect=recording_run),
            ):
                first = run_catalog_publish(config)

            self.assertTrue(first["pushed"])
            self.assertTrue(first["merged"])
            self.assertTrue(first["changed"])
            self.assertEqual(first["skipped"], [])
            self.assertEqual(fake.merged_commits, [first["commit"]])
            self.assertEqual(first["abandoned_work_directories"], 1)
            self.assertEqual(first["cleaned_work_directories"], 1)
            self.assertFalse(abandoned_work.exists())
            self.assertIn("catalog/species/1-example-bird/manifest.json", _remote_files(remote))
            self.assertEqual(_remote_branches(remote), ["main"])
            self.assertGreaterEqual(timeouts["push"], 1800)
            self.assertGreaterEqual(timeouts["fetch"], 1800)
            self.assertGreaterEqual(timeouts["worktree"], 600)
            self.assertLess(timeouts["rev-parse"], timeouts["push"])

            with _github(fake):
                second = run_catalog_publish(config)

            self.assertFalse(second["pushed"])
            self.assertFalse(second["changed"])
            self.assertEqual(second["already_present"], [1])
            self.assertEqual(second["abandoned_work_directories"], 0)
            self.assertEqual(second["cleaned_work_directories"], 0)

            _create_species(config.controller.catalog_dir, 2, "Second Bird")
            dry_run_abandoned = config.controller.state_dir / "catalog-publish-work/publish-dry-run"
            dry_run_abandoned.mkdir()
            with _github(fake):
                dry_run = run_catalog_publish(config, dry_run=True)

            self.assertTrue(dry_run["changed"])
            self.assertFalse(dry_run["pushed"])
            self.assertEqual(dry_run["abandoned_work_directories"], 1)
            self.assertEqual(dry_run["cleaned_work_directories"], 0)
            self.assertTrue(dry_run_abandoned.is_dir())
            self.assertNotIn("catalog/species/2-second-bird/manifest.json", _remote_files(remote))

            with _github(fake):
                final = run_catalog_publish(config)

            self.assertTrue(final["pushed"])
            self.assertEqual(final["cleaned_work_directories"], 1)
            self.assertFalse(dry_run_abandoned.exists())
            self.assertIn("catalog/species/2-second-bird/manifest.json", _remote_files(remote))
            self.assertEqual(fake.merged_commits, [first["commit"], final["commit"]])


class PublicationRunTests(unittest.TestCase):
    def _environment(self, root: Path, **config: str) -> tuple[Path, Path, AppConfig, _FakeGitHub]:
        remote, checkout = _initialize_remote(root)
        app_config = load_config(_write_config(root, checkout, **config))
        return remote, checkout, app_config, _FakeGitHub(self, remote)

    def test_publishes_a_species_whose_slug_is_not_ascii(self) -> None:
        with TemporaryDirectory() as temporary:
            remote, _, config, fake = self._environment(Path(temporary))
            _create_species(config.controller.catalog_dir, 1, "Rüppell Warbler")

            with _github(fake):
                result = run_catalog_publish(config)

            self.assertTrue(result["merged"])
            paths = result["paths"]
            assert isinstance(paths, list)
            self.assertIn("catalog/species/1-rüppell-warbler/manifest.json", paths)
            self.assertIn("catalog/species/1-rüppell-warbler/display.png", _remote_files(remote))
            self.assertEqual(fake.merged_commits, [result["commit"]])

    def test_skips_a_species_that_fails_publication_gates_and_publishes_the_rest(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, _, config, fake = self._environment(root)
            catalog = config.controller.catalog_dir
            _create_species(catalog, 1, "Good Bird")
            broken = _create_species(catalog, 2, "Broken Bird")
            Image.new("RGB", (1600, 1200), "black").save(broken / "display.png")

            with _github(fake):
                result = run_catalog_publish(config)

            self.assertTrue(result["merged"])
            published = [item["taxon_id"] for item in _result_items(result, "published")]
            self.assertEqual(published, [1])
            skipped = _result_items(result, "skipped")
            self.assertEqual([item["species"] for item in skipped], ["2-broken-bird"])
            reason = str(skipped[0]["reason"])
            self.assertIn("display checksum", reason)
            self.assertNotIn(str(root), reason)
            files = _remote_files(remote)
            self.assertIn("catalog/species/1-good-bird/manifest.json", files)
            self.assertFalse(any("2-broken-bird" in path for path in files))
            index = json.loads(_run_git(remote, "show", "main:catalog/index.json"))
            self.assertEqual([item["taxon_id"] for item in index["species"]], [1])
            self.assertTrue((broken / "display.png").is_file())

    def test_skips_a_new_species_that_names_the_configured_place(self) -> None:
        with TemporaryDirectory() as temporary:
            remote, _, config, fake = self._environment(Path(temporary))
            catalog = config.controller.catalog_dir
            config.controller.state_dir.mkdir(parents=True)
            write_json_atomic(
                config.controller.state_dir / "discovery.json",
                {"place_name": "Exampleville", "state": "Example State"},
            )
            _create_species(catalog, 1, "Good Bird")
            leaky = _create_species(catalog, 2, "Leaky Bird")
            _set_profile_field(
                leaky, "habitat", "Common in the parks of exampleville, example state."
            )
            rebuild_catalog_index(catalog)

            with _github(fake):
                result = run_catalog_publish(config)

            published = [item["taxon_id"] for item in _result_items(result, "published")]
            self.assertEqual(published, [1])
            skipped = _result_items(result, "skipped")
            self.assertEqual([item["species"] for item in skipped], ["2-leaky-bird"])
            self.assertIn("Private location or path", str(skipped[0]["reason"]))
            self.assertFalse(any("2-leaky-bird" in path for path in _remote_files(remote)))

    def test_commit_ignores_hooks_and_signing_and_publishes_the_validated_tree(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, checkout, config, fake = self._environment(root)
            hooks = root / "hooks"
            hooks.mkdir()
            (hooks / "pre-commit").write_text(
                "#!/bin/sh\n"
                "printf private > catalog/species/1-example-bird/notes.txt\n"
                "git add catalog/species/1-example-bird/notes.txt\n"
            )
            (hooks / "prepare-commit-msg").write_text("#!/bin/sh\nexit 1\n")
            (hooks / "pre-push").write_text("#!/bin/sh\nexit 1\n")
            for hook in hooks.iterdir():
                hook.chmod(0o755)
            _run_git(checkout, "config", "core.hooksPath", str(hooks))
            _run_git(checkout, "config", "commit.gpgSign", "true")
            _run_git(checkout, "config", "gpg.program", "/usr/bin/false")
            _create_species(config.controller.catalog_dir, 1, "Example Bird")

            with _github(fake):
                result = run_catalog_publish(config)

            self.assertTrue(result["merged"])
            files = _remote_files(remote)
            self.assertIn("catalog/species/1-example-bird/manifest.json", files)
            self.assertNotIn("catalog/species/1-example-bird/notes.txt", files)

    def test_rejects_a_commit_that_differs_from_the_validated_changes(self) -> None:
        with TemporaryDirectory() as temporary:
            remote, _, config, fake = self._environment(Path(temporary))
            _create_species(config.controller.catalog_dir, 1, "Example Bird")

            def altering_git(
                repository: Path,
                *arguments: str,
                check: bool = True,
                timeout: float = _COMMAND_TIMEOUT_SECONDS,
            ) -> subprocess.CompletedProcess[str]:
                result = _git(repository, *arguments, check=check, timeout=timeout)
                if "commit" in arguments and "-m" in arguments:
                    (repository / "catalog/extra.txt").write_text("unexpected")
                    _run_git(repository, "add", "catalog/extra.txt")
                    _run_git(
                        repository,
                        "-c",
                        "user.name=Other",
                        "-c",
                        "user.email=other@example.test",
                        "commit",
                        "--amend",
                        "--no-edit",
                    )
                return result

            with (
                _github(fake),
                patch("inky_bird_frame.publisher._git", side_effect=altering_git),
                self.assertRaisesRegex(CatalogPublishError, "unexpected change: catalog/extra.txt"),
            ):
                run_catalog_publish(config)

            self.assertEqual(_remote_branches(remote), ["main"])
            self.assertNotIn(("pr", "create"), [call_[:2] for call_ in fake.calls])

    def test_returns_before_copying_when_the_remote_already_has_everything(self) -> None:
        with TemporaryDirectory() as temporary:
            _, _, config, fake = self._environment(Path(temporary))
            _create_species(config.controller.catalog_dir, 1, "Example Bird")
            with _github(fake):
                run_catalog_publish(config)
            git_calls: list[tuple[str, ...]] = []

            def recording_git(
                repository: Path,
                *arguments: str,
                check: bool = True,
                timeout: float = _COMMAND_TIMEOUT_SECONDS,
            ) -> subprocess.CompletedProcess[str]:
                git_calls.append(arguments)
                return _git(repository, *arguments, check=check, timeout=timeout)

            with (
                _github(fake),
                patch("inky_bird_frame.publisher._git", side_effect=recording_git),
                patch("inky_bird_frame.publisher.shutil.copytree") as copytree,
            ):
                result = run_catalog_publish(config)

            copytree.assert_not_called()
            self.assertNotIn(("worktree", "add"), [item[:2] for item in git_calls])
            self.assertFalse(result["changed"])
            self.assertEqual(result["published"], [])
            self.assertEqual(result["already_present"], [1])

    def test_cleans_stale_owner_publication_branches_and_pull_requests(self) -> None:
        with TemporaryDirectory() as temporary:
            remote, _, config, fake = self._environment(Path(temporary))
            _create_species(config.controller.catalog_dir, 1, "Example Bird")
            stale_without_pull = "catalog/publish-aaaaaaaa-bbbbbbbb"
            stale_with_pull = "catalog/publish-cccccccc-dddddddd"
            foreign_pull = "catalog/publish-eeeeeeee-ffffffff"
            for branch in (stale_without_pull, stale_with_pull, foreign_pull, "feature/keep"):
                _run_git(remote, "update-ref", f"refs/heads/{branch}", "refs/heads/main")
            owner_pull = fake.add_pull_request(stale_with_pull)
            other_pull = fake.add_pull_request(foreign_pull, author="contributor")

            with _github(fake):
                result = run_catalog_publish(config)

            self.assertEqual(
                result["cleaned_publication_branches"], [stale_without_pull, stale_with_pull]
            )
            self.assertEqual(owner_pull["state"], "CLOSED")
            self.assertEqual(other_pull["state"], "OPEN")
            self.assertEqual(
                sorted(_remote_branches(remote)), sorted([foreign_pull, "feature/keep", "main"])
            )

    def test_dry_run_does_not_clean_stale_publications(self) -> None:
        with TemporaryDirectory() as temporary:
            remote, _, config, fake = self._environment(Path(temporary))
            _create_species(config.controller.catalog_dir, 1, "Example Bird")
            stale = "catalog/publish-aaaaaaaa-bbbbbbbb"
            _run_git(remote, "update-ref", f"refs/heads/{stale}", "refs/heads/main")

            with _github(fake):
                result = run_catalog_publish(config, dry_run=True)

            self.assertEqual(result["cleaned_publication_branches"], [])
            self.assertIn(stale, _remote_branches(remote))

    def test_retry_reuses_the_current_publication_branch_after_a_failed_merge(self) -> None:
        with TemporaryDirectory() as temporary:
            remote, _, config, fake = self._environment(Path(temporary))
            _create_species(config.controller.catalog_dir, 1, "Example Bird")
            fake.fail_next_merge = True
            with (
                _github(fake),
                self.assertRaisesRegex(CatalogPublishError, "simulated outage"),
            ):
                run_catalog_publish(config)
            branches = [item for item in _remote_branches(remote) if item != "main"]
            self.assertEqual(len(branches), 1)

            with _github(fake):
                result = run_catalog_publish(config)

            self.assertTrue(result["merged"])
            self.assertEqual(result["branch"], branches[0])
            self.assertEqual(result["cleaned_publication_branches"], [])
            self.assertEqual(len(fake.pull_requests), 1)
            self.assertEqual(fake.pull_requests[0]["state"], "MERGED")

    def test_publishes_a_species_named_after_the_configured_place(self) -> None:
        with TemporaryDirectory() as temporary:
            remote, _, config, fake = self._environment(Path(temporary))
            config.controller.state_dir.mkdir(parents=True)
            write_json_atomic(
                config.controller.state_dir / "discovery.json",
                {"place_name": "Baltimore", "state": "MD"},
            )
            _create_species(config.controller.catalog_dir, 1, "Baltimore Oriole")

            with _github(fake):
                result = run_catalog_publish(config)

            self.assertTrue(result["merged"])
            self.assertEqual(result["skipped"], [])
            self.assertIn("catalog/species/1-baltimore-oriole/manifest.json", _remote_files(remote))

    def test_dry_run_without_changes_does_not_clean_stale_publications(self) -> None:
        with TemporaryDirectory() as temporary:
            remote, _, config, fake = self._environment(Path(temporary))
            _create_species(config.controller.catalog_dir, 1, "Example Bird")
            with _github(fake):
                run_catalog_publish(config)
            stale = "catalog/publish-aaaaaaaa-bbbbbbbb"
            _run_git(remote, "update-ref", f"refs/heads/{stale}", "refs/heads/main")
            owner_pull = fake.add_pull_request(stale)

            with _github(fake):
                result = run_catalog_publish(config, dry_run=True)

            self.assertEqual(result["already_present"], [1])
            self.assertEqual(result["cleaned_publication_branches"], [])
            self.assertIn(stale, _remote_branches(remote))
            self.assertEqual(owner_pull["state"], "OPEN")

    def test_publishes_again_on_a_branch_that_cleanup_closed_earlier(self) -> None:
        with TemporaryDirectory() as temporary:
            remote, _, config, fake = self._environment(Path(temporary))
            catalog = config.controller.catalog_dir
            _create_species(catalog, 1, "Example Bird")
            fake.fail_next_merge = True
            with _github(fake), self.assertRaisesRegex(CatalogPublishError, "simulated outage"):
                run_catalog_publish(config)
            first_branch = str(fake.pull_requests[0]["headRefName"])

            second = _create_species(catalog, 2, "Second Bird")
            fake.fail_next_merge = True
            with _github(fake), self.assertRaisesRegex(CatalogPublishError, "simulated outage"):
                run_catalog_publish(config)
            self.assertEqual(fake.pull_requests[0]["state"], "CLOSED")
            self.assertNotIn(first_branch, _remote_branches(remote))

            shutil.rmtree(second)
            rebuild_catalog_index(catalog)
            with _github(fake):
                result = run_catalog_publish(config)

            self.assertTrue(result["merged"])
            self.assertEqual(result["branch"], first_branch)
            self.assertEqual(
                [item["state"] for item in fake.pull_requests], ["CLOSED", "CLOSED", "MERGED"]
            )
            self.assertIn("catalog/species/1-example-bird/manifest.json", _remote_files(remote))

    def test_recycled_branch_publishes_after_pull_request_creation_failed(self) -> None:
        with TemporaryDirectory() as temporary:
            remote, _, config, fake = self._environment(Path(temporary))
            catalog = config.controller.catalog_dir
            _create_species(catalog, 1, "Example Bird")
            fake.fail_next_merge = True
            with _github(fake), self.assertRaisesRegex(CatalogPublishError, "simulated outage"):
                run_catalog_publish(config)
            first_branch = str(fake.pull_requests[0]["headRefName"])
            closed_head = fake.pull_requests[0]["headRefOid"]

            second = _create_species(catalog, 2, "Second Bird")
            fake.fail_next_merge = True
            with _github(fake), self.assertRaisesRegex(CatalogPublishError, "simulated outage"):
                run_catalog_publish(config)
            self.assertEqual(fake.pull_requests[0]["state"], "CLOSED")

            shutil.rmtree(second)
            rebuild_catalog_index(catalog)
            fake.fail_next_create = True
            later = "2030-01-01T00:00:00+00:00"
            with (
                _github(fake),
                patch.dict(os.environ, {"GIT_AUTHOR_DATE": later, "GIT_COMMITTER_DATE": later}),
                self.assertRaisesRegex(CatalogPublishError, "simulated outage"),
            ):
                run_catalog_publish(config)
            self.assertIn(first_branch, _remote_branches(remote))
            self.assertNotEqual(
                _run_git(remote, "rev-parse", f"refs/heads/{first_branch}"), closed_head
            )

            with _github(fake):
                result = run_catalog_publish(config)

            self.assertTrue(result["merged"])
            self.assertEqual(result["branch"], first_branch)
            self.assertEqual(
                [item["state"] for item in fake.pull_requests], ["CLOSED", "CLOSED", "MERGED"]
            )

    def test_refuses_a_closed_pull_request_whose_branch_was_kept(self) -> None:
        with TemporaryDirectory() as temporary:
            remote, _, config, fake = self._environment(Path(temporary))
            _create_species(config.controller.catalog_dir, 1, "Example Bird")
            fake.fail_next_merge = True
            with _github(fake), self.assertRaisesRegex(CatalogPublishError, "simulated outage"):
                run_catalog_publish(config)
            fake.pull_requests[0]["state"] = "CLOSED"
            branch = str(fake.pull_requests[0]["headRefName"])

            with (
                _github(fake),
                self.assertRaisesRegex(CatalogPublishError, "closed without merge"),
            ):
                run_catalog_publish(config)

            self.assertIn(branch, _remote_branches(remote))
            self.assertEqual(len(fake.pull_requests), 1)

    def test_cleanup_failure_does_not_replace_the_original_error(self) -> None:
        with TemporaryDirectory() as temporary:
            _, _, config, fake = self._environment(Path(temporary))
            _create_species(config.controller.catalog_dir, 1, "Example Bird")

            def failing_git(
                repository: Path,
                *arguments: str,
                check: bool = True,
                timeout: float = _COMMAND_TIMEOUT_SECONDS,
            ) -> subprocess.CompletedProcess[str]:
                if arguments[:1] == ("push",) and "--delete" not in arguments:
                    raise CatalogPublishError("git push timed out after 3600 seconds")
                if arguments[:2] == ("worktree", "remove"):
                    raise CatalogPublishError("git worktree timed out after 900 seconds")
                return _git(repository, *arguments, check=check, timeout=timeout)

            with (
                _github(fake),
                patch("inky_bird_frame.publisher._git", side_effect=failing_git),
                self.assertRaisesRegex(CatalogPublishError, "^git push timed out"),
            ):
                run_catalog_publish(config)


class CommandErrorTests(unittest.TestCase):
    def test_git_errors_name_the_subcommand(self) -> None:
        cases: list[tuple[object, str]] = [
            (subprocess.TimeoutExpired(["git"], 5), "^git push timed out after [0-9]+ seconds$"),
            (FileNotFoundError("git"), "^Unable to run git push$"),
            (
                subprocess.CompletedProcess(["git"], 1, "", "fatal: boom\n"),
                "^git push failed: fatal: boom$",
            ),
        ]
        for outcome, message in cases:
            side_effect = outcome if isinstance(outcome, BaseException) else None
            return_value = None if side_effect is not None else outcome
            with (
                self.subTest(message=message),
                patch(
                    "inky_bird_frame.publisher.subprocess.run",
                    side_effect=side_effect,
                    return_value=return_value,
                ),
                self.assertRaisesRegex(CatalogPublishError, message),
            ):
                _git(Path("checkout"), "-c", "core.quotePath=false", "push", "origin", "HEAD")

    def test_gh_errors_name_the_subcommand(self) -> None:
        with (
            patch(
                "inky_bird_frame.publisher.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["gh"], 5),
            ),
            self.assertRaisesRegex(CatalogPublishError, "^gh pr merge timed out after"),
        ):
            _gh(PublicCatalogConfig(), "pr", "merge", "https://github.com/example/pull/1")


class PublicationPrivacyTests(unittest.TestCase):
    def test_rejects_png_chunks_after_the_image_data(self) -> None:
        cases = {
            "text after image data": _png_chunk(b"tEXt", b"Comment\x00lat 1.0 lon 2.0"),
            "private chunk": _png_chunk(b"prVt", b"private"),
            "time chunk": _png_chunk(b"tIME", b"\x07\xea\x01\x01\x00\x00\x00"),
        }
        for label, chunk in cases.items():
            with self.subTest(label=label), TemporaryDirectory() as temporary:
                catalog = Path(temporary) / "catalog"
                directory = _create_species(catalog, 1, "Example Bird")
                _insert_png_chunk_before_end(directory / "portrait.png", chunk)
                _refresh_asset_checksums(directory)

                with self.assertRaisesRegex(CatalogPublishError, "metadata is not allowed"):
                    validate_public_catalog(catalog)

    def test_rejects_bytes_after_the_png_end(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "display.png"
            Image.new("RGB", (1600, 1200), "white").save(path)
            path.write_bytes(path.read_bytes() + b"lat 1.0 lon 2.0")

            with self.assertRaisesRegex(CatalogPublishError, "metadata is not allowed"):
                _validate_image(path, (1600, 1200))

    def test_accepts_a_plain_png(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "display.png"
            Image.new("RGB", (1600, 1200), "white").save(path)
            _validate_image(path, (1600, 1200))
            Image.new("P", (1600, 1200), 3).save(path)
            _validate_image(path, (1600, 1200))

    def _config(self, root: Path, location: str) -> AppConfig:
        checkout = root / "checkout"
        checkout.mkdir(exist_ok=True)
        config = load_config(_write_config(root, checkout, location=location))
        config.controller.state_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(
            config.controller.state_dir / "discovery.json",
            {"place_name": "Exampleville", "state": "Example State"},
        )
        return config

    def test_rejects_configured_location_and_paths_inside_free_text(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            postal_config = self._config(root, 'zip_code = "12345"')
            postal = publication_privacy_patterns(postal_config)
            coordinates = publication_privacy_patterns(
                self._config(root, "latitude = 1.234567\nlongitude = -2.345678")
            )
            state_dir = postal_config.controller.state_dir
            rejected = {
                "place": (postal, "Often seen near Exampleville, Example State in winter."),
                "place without comma": (postal, "exampleville example state parks"),
                "postal code": (postal, "Area 12345 survey"),
                "state path": (postal, f"Saved under {state_dir}/pending"),
                "home path": (postal, f"Copied from {Path.home()}/Pictures"),
                "home path at sentence end": (postal, f"Copied from {Path.home()}."),
                "rounded latitude": (coordinates, "lat 1.2346, lon 0"),
                "truncated longitude": (coordinates, "lon -2.345 here"),
                "longitude digits": (coordinates, "at 2.345678 degrees"),
            }
            for label, (patterns, text) in rejected.items():
                with (
                    self.subTest(label=label),
                    self.assertRaisesRegex(CatalogPublishError, "Private location or path"),
                ):
                    _check_json_privacy(
                        {"profile": {"sources": [{"title": text}]}},
                        Path("manifest.json"),
                        patterns,
                    )

            for text in (
                "ab12345cd",
                "Wingspan 12.345 cm",
                "https://doi.org/10.1234/example",
                "Example State woodland",
                "The Exampleville Warbler sings",
                "https://example.test/one",
            ):
                with self.subTest(allowed=text):
                    _check_json_privacy({"habitat": text}, Path("profile.json"), postal)
                    _check_json_privacy({"habitat": text}, Path("profile.json"), coordinates)

    def test_privacy_patterns_tolerate_missing_discovery_state(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "checkout"
            checkout.mkdir()
            config = load_config(_write_config(root, checkout))
            patterns = publication_privacy_patterns(config)
            with self.assertRaisesRegex(CatalogPublishError, "Private location or path"):
                _check_json_privacy("zip 12345", Path("profile.json"), patterns)
            _check_json_privacy("exampleville", Path("profile.json"), patterns)

    def test_place_names_inside_species_names_are_not_private(self) -> None:
        cases = {
            ("Baltimore", "MD"): (
                ["The Baltimore Oriole sings", "Baltimore Oriole (Icterus galbula)"],
                ["Nests in Baltimore, MD parks", "baltimore md"],
            ),
            ("Lincoln", "NE"): (["Lincoln's Sparrow skulks in brush"], ["Lincoln, NE"]),
            ("United States", ""): (["Breeds across the United States and Canada"], []),
        }
        for (place, state), (allowed, rejected) in cases.items():
            with self.subTest(place=place), TemporaryDirectory() as temporary:
                config = self._config(Path(temporary), 'zip_code = "12345"')
                write_json_atomic(
                    config.controller.state_dir / "discovery.json",
                    {"place_name": place, "state": state},
                )
                patterns = publication_privacy_patterns(config)
                for text in allowed:
                    _check_json_privacy({"common_name": text}, Path("profile.json"), patterns)
                for text in rejected:
                    with self.assertRaisesRegex(CatalogPublishError, "Private location or path"):
                        _check_json_privacy({"habitat": text}, Path("profile.json"), patterns)

    def test_paths_do_not_match_a_longer_sibling(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary), 'zip_code = "12345"')
            patterns = publication_privacy_patterns(config)
            home = Path.home()
            for text in (f"Notes on {home}stein", f"Copied from {home}-old/file"):
                with self.subTest(text=text):
                    _check_json_privacy({"title": text}, Path("profile.json"), patterns)
            with self.assertRaisesRegex(CatalogPublishError, "Private location or path"):
                _check_json_privacy(
                    {"title": f"Copied from {home}/x"}, Path("profile.json"), patterns
                )

    def test_truncated_coordinates_do_not_gain_float_error_terms(self) -> None:
        terms = _coordinate_terms(40.123)
        self.assertIn("40.123", terms)
        self.assertIn("40.12300", terms)
        self.assertNotIn("40.12299", terms)
        self.assertNotIn("0.28999", _coordinate_terms(0.29))
        self.assertIn("2.345", _coordinate_terms(-2.3459))


if __name__ == "__main__":
    unittest.main()
