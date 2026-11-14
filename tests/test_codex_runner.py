from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from inky_bird_frame import codex_runner
from inky_bird_frame.birds import BirdSpecies, TaxonContext
from inky_bird_frame.codex_runner import (
    CodexRunner,
    parse_species_profile,
)
from inky_bird_frame.codex_runner import (
    _parse_review as _parse_review_impl,
)
from inky_bird_frame.errors import GenerationError
from inky_bird_frame.models import QualityReview, SpeciesProfileData

_CAPTURE_OUTPUT_ARGUMENT = """\
out=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "-o" ]; then
    out="$arg"
  fi
  prev="$arg"
done
"""


def _stub_executable(root: Path, body: str) -> Path:
    stub = root / "codex"
    stub.write_text(f"#!/bin/sh\n{body}\n")
    stub.chmod(0o755)
    return stub


def _species() -> BirdSpecies:
    return BirdSpecies(1, "Test Bird", "Avis test", 1, "test")


def _context() -> TaxonContext:
    return TaxonContext(
        taxon_id=1,
        common_name="Test Bird",
        scientific_name="Avis test",
        family="Testidae",
        summary="A test bird.",
        source_url="https://birds.example/taxa/1",
    )


def _profile() -> SpeciesProfileData:
    return SpeciesProfileData(
        taxon_id=1,
        common_name="Test Bird",
        scientific_name="Avis test",
        family="Testidae",
        measurements={"length": "1 in", "wingspan": "2 in", "weight": "3 oz"},
        field_marks=["one", "two", "three", "four"],
        habitat="Woods",
        behavior="Perches",
        palette=["red", "green", "blue"],
        sources=[
            {"title": "One", "url": "https://birds.example/one"},
            {"title": "Two", "url": "https://field.example/two"},
        ],
    )


def _parse_review(
    raw: object,
    allowed_domains: tuple[str, ...] | None = None,
    *,
    prior_corrections: tuple[str, ...] = (),
) -> QualityReview:
    return _parse_review_impl(
        raw,
        _profile(),
        allowed_domains,
        prior_corrections=prior_corrections,
    )


class CodexRunnerSubprocessTests(unittest.TestCase):
    def test_nonzero_exit_raises_and_still_writes_log(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stub = _stub_executable(
                root,
                'echo "stub stdout marker"\necho "stub stderr marker" >&2\nexit 3',
            )
            runner = CodexRunner(stub, root)
            log_path = root / "logs" / "plate.log"

            with self.assertRaisesRegex(GenerationError, "Codex exited with status 3; see "):
                runner.generate_plate(_species(), _profile(), [], [], root / "plate.png", log_path)

            self.assertTrue(log_path.is_file())
            log = log_path.read_text()
            self.assertIn("stub stdout marker", log)
            self.assertIn("stub stderr marker", log)

    def test_timeout_raises_generation_error(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stub = _stub_executable(root, "sleep 5")
            runner = CodexRunner(stub, root, timeout_seconds=1)

            with self.assertRaisesRegex(GenerationError, "Codex timed out after 1 second"):
                runner.generate_plate(
                    _species(), _profile(), [], [], root / "plate.png", root / "plate.log"
                )

    def test_structured_output_missing_file_raises(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stub = _stub_executable(root, "exit 0")
            runner = CodexRunner(stub, root)
            plate_path = root / "plate.png"
            plate_path.write_bytes(b"png")

            with self.assertRaisesRegex(
                GenerationError, "Codex did not write valid structured output"
            ):
                runner.review_plate(
                    _species(),
                    _profile(),
                    [],
                    plate_path,
                    [],
                    root / "review.json",
                    root / "review.log",
                    allowed_domains=("birds.example", "field.example"),
                )

    def test_structured_output_invalid_json_raises(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stub = _stub_executable(root, f'{_CAPTURE_OUTPUT_ARGUMENT}printf "not json" > "$out"')
            runner = CodexRunner(stub, root)

            with self.assertRaisesRegex(
                GenerationError, "Codex did not write valid structured output"
            ):
                runner.create_profile(
                    _species(),
                    _context(),
                    [],
                    [],
                    root / "profile.json",
                    root / "profile.log",
                    allowed_domains=("birds.example", "field.example"),
                )

    def test_generate_plate_missing_image_raises(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stub = _stub_executable(root, "exit 0")
            runner = CodexRunner(stub, root)

            with self.assertRaisesRegex(
                GenerationError, "Codex did not create the requested plate"
            ):
                runner.generate_plate(
                    _species(), _profile(), [], [], root / "plate.png", root / "plate.log"
                )

    def test_generate_plate_zero_byte_image_raises(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stub = _stub_executable(root, "exit 0")
            runner = CodexRunner(stub, root)
            plate_path = root / "plate.png"
            plate_path.touch()

            with self.assertRaisesRegex(
                GenerationError, "Codex did not create the requested plate"
            ):
                runner.generate_plate(
                    _species(), _profile(), [], [], plate_path, root / "plate.log"
                )

    def test_generate_plate_attaches_correction_source_before_references(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "codex"
            executable.touch()
            source = root / "source.png"
            reference = root / "reference.png"
            source.write_bytes(b"source")
            reference.write_bytes(b"reference")
            output = root / "plate.png"
            runner = CodexRunner(executable, root)

            def run(_command: list[str], _prompt: str, _log_path: Path) -> None:
                output.write_bytes(b"generated")

            with patch.object(runner, "_run", side_effect=run) as execute:
                runner.generate_plate(
                    _species(),
                    _profile(),
                    [],
                    [reference],
                    output,
                    root / "plate.log",
                    ("Shorten the tail",),
                    correction_source_path=source,
                    invariant_findings=("Keep the correct wing pattern",),
                )

        command = execute.call_args.args[0]
        images = [command[index + 1] for index, value in enumerate(command) if value == "--image"]
        self.assertEqual(images, [str(source.resolve()), str(reference.resolve())])
        self.assertIn(
            "Image 1 is the previous plate and the edit target", execute.call_args.args[1]
        )
        self.assertIn("Shorten the tail", execute.call_args.args[1])
        self.assertIn("Keep the correct wing pattern", execute.call_args.args[1])
        self.assertIn("Non-regression constraints", execute.call_args.args[1])

    def test_create_profile_rejects_mismatched_taxon_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload: dict[str, object] = dict(_profile())
            payload["taxon_id"] = 999
            payload_path = root / "payload.json"
            payload_path.write_text(json.dumps(payload))
            stub = _stub_executable(root, f'{_CAPTURE_OUTPUT_ARGUMENT}cp "{payload_path}" "$out"')
            runner = CodexRunner(stub, root)

            with self.assertRaisesRegex(
                GenerationError, "Codex profile identity does not match the discovered taxon"
            ):
                runner.create_profile(
                    _species(),
                    _context(),
                    [],
                    [],
                    root / "profile.json",
                    root / "profile.log",
                    allowed_domains=("birds.example", "field.example"),
                )


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_exit(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            return True
        time.sleep(0.05)
    return not _process_exists(pid)


class CodexRunnerProcessSafetyTests(unittest.TestCase):
    def test_child_environment_excludes_job_secrets(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture = root / "environment.txt"
            stub = _stub_executable(root, f'env > "{capture}"')
            runner = CodexRunner(stub, root)
            job_environment = {
                "INKY_TEST_PROVIDER_SECRET": "sentinel-provider-secret",
                "EBIRD_API_KEY": "sentinel-ebird-key",
                "INKY_BIRD_OIDC_CLIENT_SECRET": "sentinel-oidc-secret",
                "HTTPS_PROXY": "http://proxy.example.test:3128",
                "SSL_CERT_FILE": "/etc/ssl/example-bundle.pem",
                "CODEX_HOME": "/srv/example/codex-home",
                "LC_CTYPE": "C.UTF-8",
            }

            with patch.dict(os.environ, job_environment):
                runner._run([str(stub)], "prompt", root / "run.log")

            child_text = capture.read_text()
            child = dict(line.split("=", 1) for line in child_text.splitlines() if "=" in line)
        # Report only names: a failure message must never print environment values.
        self.assertFalse("sentinel-" in child_text, "a job secret value reached the child")
        self.assertEqual(
            sorted(name for name in child if name in job_environment and "SECRET" in name),
            [],
        )
        self.assertNotIn("EBIRD_API_KEY", list(child))
        self.assertEqual(child.get("HTTPS_PROXY"), "http://proxy.example.test:3128")
        self.assertEqual(child.get("SSL_CERT_FILE"), "/etc/ssl/example-bundle.pem")
        self.assertEqual(child.get("CODEX_HOME"), "/srv/example/codex-home")
        self.assertEqual(child.get("LC_CTYPE"), "C.UTF-8")
        self.assertEqual(child.get("PATH"), os.environ["PATH"])
        self.assertIn("HOME", list(child))

    def test_timeout_kills_the_whole_process_group(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pid_file = root / "grandchild.pid"
            stub = _stub_executable(
                root,
                f"(trap '' TERM; exec sleep 60) &\necho $! > \"{pid_file}\"\nwait",
            )
            runner = CodexRunner(stub, root, timeout_seconds=1)

            with (
                patch.object(codex_runner, "TERMINATE_GRACE_SECONDS", 0.5),
                self.assertRaisesRegex(GenerationError, "Codex timed out after 1 seconds"),
            ):
                runner._run([str(stub)], "prompt", root / "run.log")

            grandchild = int(pid_file.read_text())
            survived = not _wait_for_exit(grandchild, 5.0)
            if survived:
                os.kill(grandchild, 9)
        self.assertFalse(survived, "a grandchild of the Codex process survived its timeout")

    def test_timeout_logs_captured_output_and_names_the_log(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stub = _stub_executable(
                root, 'echo "early stdout marker"\necho "early stderr marker" >&2\nexec sleep 60'
            )
            runner = CodexRunner(stub, root, timeout_seconds=1)
            log_path = root / "logs" / "run.log"

            with self.assertRaises(GenerationError) as raised:
                runner._run([str(stub)], "prompt", log_path)

            self.assertIn("timed out", str(raised.exception))
            self.assertIn(str(log_path), str(raised.exception))
            log = log_path.read_text()
        self.assertIn("early stdout marker", log)
        self.assertIn("early stderr marker", log)

    def test_captured_output_is_bounded(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stub = _stub_executable(
                root, "head -c 2000000 /dev/zero | tr '\\0' a\necho tail-marker"
            )
            runner = CodexRunner(stub, root)
            log_path = root / "run.log"

            with patch.object(codex_runner, "MAX_CAPTURED_OUTPUT_BYTES", 4096):
                runner._run([str(stub)], "prompt", log_path)

            size = log_path.stat().st_size
            log = log_path.read_text()
        self.assertLess(size, 16384)
        self.assertIn("output truncated", log)

    def test_unstartable_executable_raises_generation_error(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "codex"
            executable.write_text("not a program\n")
            executable.chmod(0o644)
            runner = CodexRunner(executable, root)

            with self.assertRaisesRegex(GenerationError, "Codex could not be started"):
                runner._run([str(executable)], "prompt", root / "run.log")

    def test_undecodable_output_is_logged_without_failing(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stub = _stub_executable(root, "printf 'bad \\377\\376 bytes\\n'")
            runner = CodexRunner(stub, root)
            log_path = root / "run.log"

            runner._run([str(stub)], "prompt", log_path)

            log = log_path.read_text()
        self.assertIn("bad", log)
        self.assertIn("\ufffd", log)

    def test_stale_structured_output_is_not_reused(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stub = _stub_executable(root, "exit 0")
            runner = CodexRunner(stub, root)
            output_path = root / "profile.json"
            output_path.write_text(json.dumps(dict(_profile())))

            with self.assertRaisesRegex(
                GenerationError, "Codex did not write valid structured output"
            ):
                runner.create_profile(
                    _species(),
                    _context(),
                    [],
                    [],
                    output_path,
                    root / "profile.log",
                    allowed_domains=("birds.example", "field.example"),
                )

    def test_undecodable_structured_output_raises_generation_error(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stub = _stub_executable(
                root, f"{_CAPTURE_OUTPUT_ARGUMENT}printf '\\377\\376' > \"$out\""
            )
            runner = CodexRunner(stub, root)

            with self.assertRaisesRegex(
                GenerationError, "Codex did not write valid structured output"
            ):
                runner.create_profile(
                    _species(),
                    _context(),
                    [],
                    [],
                    root / "profile.json",
                    root / "profile.log",
                    allowed_domains=("birds.example", "field.example"),
                )


def _profile_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = dict(_profile())
    payload.update(changes)
    return payload


def _passing_review_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "passed": True,
        "species_accuracy": 5,
        "anatomy_accuracy": 5,
        "text_accuracy": 5,
        "composition_quality": 5,
        "location_free": True,
        "findings": ["Accurate plumage"],
        "correction_findings": [],
        "verification_sources": [
            {"title": "One", "url": "https://birds.example/one"},
            {"title": "Two", "url": "https://field.example/two"},
        ],
    }
    payload.update(changes)
    return payload


class CodexOutputSizeTests(unittest.TestCase):
    def test_profile_accepts_generous_real_world_sizes(self) -> None:
        profile = parse_species_profile(
            _profile_payload(
                habitat="h" * 3000,
                behavior="b" * 3000,
                field_marks=[f"mark {index} " + "m" * 400 for index in range(20)],
                palette=[f"colour {index}" for index in range(20)],
            )
        )

        self.assertEqual(len(profile["field_marks"]), 20)

    def test_profile_rejects_runaway_strings(self) -> None:
        cases: dict[str, object] = {
            "common_name": "n" * 201,
            "family": "f" * 201,
            "habitat": "h" * 4001,
            "behavior": "b" * 4001,
            "measurements": {"length": "l" * 501, "wingspan": "2 in", "weight": "3 oz"},
            "field_marks": ["one", "two", "three", "f" * 1001],
            "palette": ["red", "green", "p" * 301],
            "sources": [
                {"title": "t" * 501, "url": "https://birds.example/one"},
                {"title": "Two", "url": "https://field.example/two"},
            ],
        }
        for field, value in cases.items():
            with self.subTest(field=field), self.assertRaisesRegex(GenerationError, "at most"):
                parse_species_profile(_profile_payload(**{field: value}))

    def test_profile_rejects_runaway_lists(self) -> None:
        sources = [
            {"title": f"Source {index}", "url": f"https://birds.example/{index}"}
            for index in range(33)
        ]
        cases: dict[str, object] = {
            "field_marks": [f"mark {index}" for index in range(33)],
            "palette": [f"colour {index}" for index in range(33)],
            "sources": sources,
        }
        for field, value in cases.items():
            with self.subTest(field=field), self.assertRaisesRegex(GenerationError, "at most"):
                parse_species_profile(_profile_payload(**{field: value}))

    def test_review_rejects_runaway_findings(self) -> None:
        cases: dict[str, object] = {
            "findings": ["x" * 4001],
            "verification_sources": [
                {"title": f"Source {index}", "url": f"https://birds.example/{index}"}
                for index in range(33)
            ],
        }
        for field, value in cases.items():
            with self.subTest(field=field), self.assertRaisesRegex(GenerationError, "at most"):
                _parse_review(_passing_review_payload(**{field: value}))
        with self.assertRaisesRegex(GenerationError, "at most"):
            _parse_review(_passing_review_payload(findings=["finding"] * 101))
        with self.assertRaisesRegex(GenerationError, "at most"):
            _parse_review(
                _passing_review_payload(
                    passed=False,
                    species_accuracy=3,
                    correction_findings=["Fix the bill " + "c" * 4000],
                )
            )
        with self.assertRaisesRegex(GenerationError, "at most"):
            _parse_review(
                _passing_review_payload(
                    passed=False,
                    species_accuracy=3,
                    correction_findings=[f"Fix part {index}" for index in range(51)],
                )
            )

    def test_review_rejects_runaway_profile_conflict(self) -> None:
        conflict = {
            "field": "habitat",
            "profile_value": "Woods",
            "observed_value": "o" * 4001,
            "sources": [
                {"title": "One", "url": "https://birds.example/habitat"},
                {"title": "Two", "url": "https://field.example/habitat"},
            ],
        }
        with self.assertRaisesRegex(GenerationError, "at most"):
            _parse_review(
                _passing_review_payload(
                    passed=False, species_accuracy=3, profile_conflicts=[conflict]
                )
            )

    def test_structured_output_file_is_bounded(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stub = _stub_executable(
                root,
                f"{_CAPTURE_OUTPUT_ARGUMENT}head -c 2000000 /dev/zero | tr '\\0' ' ' > \"$out\"",
            )
            runner = CodexRunner(stub, root)

            with self.assertRaisesRegex(GenerationError, "structured output is too large"):
                runner.create_profile(
                    _species(),
                    _context(),
                    [],
                    [],
                    root / "profile.json",
                    root / "profile.log",
                    allowed_domains=("birds.example", "field.example"),
                )


class CodexRunnerTests(unittest.TestCase):
    def test_configured_model_is_forwarded_to_exec(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "codex"
            executable.touch()
            command = CodexRunner(executable, root, model="tested-model")._base_command(
                writable=False
            )

        self.assertEqual(command[command.index("--model") + 1], "tested-model")

    def test_review_uses_bounded_live_source_verification(self) -> None:
        species = BirdSpecies(1, "Test Bird", "Avis test", 1, "test")
        profile = SpeciesProfileData(
            taxon_id=1,
            common_name="Test Bird",
            scientific_name="Avis test",
            family="Testidae",
            measurements={"length": "1 in", "wingspan": "2 in", "weight": "3 oz"},
            field_marks=["one", "two", "three", "four"],
            habitat="Woods",
            behavior="Perches",
            palette=["red", "green", "blue"],
            sources=[
                {"title": "One", "url": "https://birds.example/one"},
                {"title": "Two", "url": "https://field.example/two"},
            ],
        )
        raw_review = {
            "passed": True,
            "species_accuracy": 5,
            "anatomy_accuracy": 5,
            "text_accuracy": 5,
            "composition_quality": 5,
            "location_free": True,
            "findings": [],
            "correction_findings": [],
            "verification_sources": profile["sources"],
        }
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "codex"
            executable.touch()
            runner = CodexRunner(executable, root)
            with patch.object(runner, "_structured", return_value=raw_review) as structured:
                runner.review_plate(
                    species,
                    profile,
                    [],
                    Path("plate.png"),
                    [],
                    Path("review.json"),
                    Path("review.log"),
                    allowed_domains=("birds.example", "field.example"),
                )

        self.assertTrue(structured.call_args.kwargs["search"])
        prompt = structured.call_args.args[0]
        self.assertIn("birds.example, field.example", prompt)

    def test_review_requires_two_verification_sources(self) -> None:
        with self.assertRaisesRegex(GenerationError, "at least two verification sources"):
            _parse_review(
                {
                    "passed": True,
                    "species_accuracy": 5,
                    "anatomy_accuracy": 5,
                    "text_accuracy": 5,
                    "composition_quality": 5,
                    "location_free": True,
                    "findings": [],
                    "correction_findings": [],
                    "verification_sources": [
                        {"title": "Cornell", "url": "https://www.allaboutbirds.org/example"}
                    ],
                }
            )

    def test_review_passes_with_scores_and_two_verification_sources(self) -> None:
        review = _parse_review(
            {
                "passed": True,
                "species_accuracy": 5,
                "anatomy_accuracy": 4,
                "text_accuracy": 5,
                "composition_quality": 4,
                "location_free": True,
                "findings": [],
                "correction_findings": [],
                "verification_sources": [
                    {"title": "Cornell", "url": "https://www.allaboutbirds.org/example"},
                    {"title": "Audubon", "url": "https://www.audubon.org/example"},
                ],
            }
        )

        self.assertTrue(review.passed)
        self.assertEqual(len(review.verification_sources), 2)

    def test_review_requires_two_distinct_verification_urls(self) -> None:
        with self.assertRaisesRegex(GenerationError, "at least two verification sources"):
            _parse_review(
                {
                    "passed": True,
                    "species_accuracy": 5,
                    "anatomy_accuracy": 5,
                    "text_accuracy": 5,
                    "composition_quality": 5,
                    "location_free": True,
                    "findings": [],
                    "correction_findings": [],
                    "verification_sources": [
                        {
                            "title": "Cornell identification",
                            "url": "https://example.test/bird",
                        },
                        {
                            "title": "Cornell life history",
                            "url": "https://example.test/bird",
                        },
                    ],
                }
            )

    def test_noninteractive_command_allows_deployment_workspace(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "codex"
            executable.touch()
            runner = CodexRunner(executable, root)

            command = runner._base_command(writable=False)

        self.assertIn("--skip-git-repo-check", command)
        self.assertIn("read-only", command)

    def test_profile_rejects_sources_outside_allowlist(self) -> None:
        profile = {
            "taxon_id": 1,
            "common_name": "Test Bird",
            "scientific_name": "Avis test",
            "family": "Testidae",
            "measurements": {"length": "1 in", "wingspan": "2 in", "weight": "3 oz"},
            "field_marks": ["one", "two", "three", "four"],
            "habitat": "Woods",
            "behavior": "Perches",
            "palette": ["red", "green", "blue"],
            "sources": [
                {"title": "Allowed", "url": "https://birds.example/one"},
                {"title": "Not allowed", "url": "https://search.example/two"},
            ],
        }

        with self.assertRaisesRegex(GenerationError, "allowlist"):
            parse_species_profile(profile, ("birds.example",))

    def test_review_requires_independent_source_domains(self) -> None:
        with self.assertRaisesRegex(GenerationError, "independent verification source domains"):
            _parse_review(
                {
                    "passed": True,
                    "species_accuracy": 5,
                    "anatomy_accuracy": 5,
                    "text_accuracy": 5,
                    "composition_quality": 5,
                    "location_free": True,
                    "findings": [],
                    "correction_findings": [],
                    "verification_sources": [
                        {"title": "One", "url": "https://birds.example/one"},
                        {"title": "Two", "url": "https://birds.example/two"},
                    ],
                }
            )

    def test_failed_review_requires_actionable_corrections(self) -> None:
        with self.assertRaisesRegex(GenerationError, "must include correction_findings"):
            _parse_review(
                {
                    "passed": False,
                    "species_accuracy": 3,
                    "anatomy_accuracy": 4,
                    "text_accuracy": 5,
                    "composition_quality": 4,
                    "location_free": True,
                    "findings": ["The bill is too long"],
                    "correction_findings": [],
                    "verification_sources": [
                        {"title": "Cornell", "url": "https://www.allaboutbirds.org/example"},
                        {"title": "Audubon", "url": "https://www.audubon.org/example"},
                    ],
                }
            )

    def test_passing_review_rejects_actionable_corrections(self) -> None:
        with self.assertRaisesRegex(GenerationError, "must not include correction_findings"):
            _parse_review(
                {
                    "passed": True,
                    "species_accuracy": 5,
                    "anatomy_accuracy": 4,
                    "text_accuracy": 5,
                    "composition_quality": 4,
                    "location_free": True,
                    "findings": ["No material issue"],
                    "correction_findings": ["Change the bill"],
                    "verification_sources": [
                        {"title": "Cornell", "url": "https://www.allaboutbirds.org/example"},
                        {"title": "Audubon", "url": "https://www.audubon.org/example"},
                    ],
                }
            )

    def test_review_parses_source_backed_profile_conflict(self) -> None:
        review = _parse_review(
            {
                "passed": False,
                "species_accuracy": 5,
                "anatomy_accuracy": 5,
                "text_accuracy": 3,
                "composition_quality": 5,
                "location_free": True,
                "findings": ["The cached length conflicts with both sources"],
                "correction_findings": [],
                "profile_conflicts": [
                    {
                        "field": "measurements.length",
                        "profile_value": "1 in",
                        "observed_value": "12-15 cm",
                        "sources": [
                            {"title": "Birds", "url": "https://birds.example/length"},
                            {"title": "Field", "url": "https://field.example/length"},
                        ],
                    }
                ],
                "verification_sources": [
                    {"title": "Birds", "url": "https://birds.example/length"},
                    {"title": "Field", "url": "https://field.example/length"},
                ],
            },
            ("birds.example", "field.example"),
        )

        self.assertFalse(review.passed)
        self.assertEqual(review.profile_conflicts[0]["field"], "measurements.length")

    def test_profile_conflict_rejects_identity_field(self) -> None:
        with self.assertRaisesRegex(GenerationError, "field is not supported"):
            _parse_review(
                {
                    "passed": False,
                    "species_accuracy": 3,
                    "anatomy_accuracy": 5,
                    "text_accuracy": 5,
                    "composition_quality": 5,
                    "location_free": True,
                    "findings": ["Identity conflict"],
                    "correction_findings": [],
                    "profile_conflicts": [
                        {
                            "field": "scientific_name",
                            "profile_value": "Avis one",
                            "observed_value": "Avis two",
                            "sources": [
                                {"title": "Birds", "url": "https://birds.example/name"},
                                {"title": "Field", "url": "https://field.example/name"},
                            ],
                        }
                    ],
                    "verification_sources": [
                        {"title": "Birds", "url": "https://birds.example/name"},
                        {"title": "Field", "url": "https://field.example/name"},
                    ],
                },
                ("birds.example", "field.example"),
            )

    def test_profile_conflict_requires_independent_sources(self) -> None:
        with self.assertRaisesRegex(GenerationError, "two independent verification sources"):
            _parse_review(
                {
                    "passed": False,
                    "species_accuracy": 5,
                    "anatomy_accuracy": 5,
                    "text_accuracy": 3,
                    "composition_quality": 5,
                    "location_free": True,
                    "findings": ["Length conflict"],
                    "correction_findings": [],
                    "profile_conflicts": [
                        {
                            "field": "measurements.length",
                            "profile_value": "1 in",
                            "observed_value": "12-15 cm",
                            "sources": [
                                {"title": "One", "url": "https://birds.example/one"},
                                {"title": "Two", "url": "https://birds.example/two"},
                            ],
                        }
                    ],
                    "verification_sources": [
                        {"title": "Birds", "url": "https://birds.example/length"},
                        {"title": "Field", "url": "https://field.example/length"},
                    ],
                },
                ("birds.example", "field.example"),
            )

    def test_profile_conflict_must_quote_the_current_profile(self) -> None:
        with self.assertRaisesRegex(GenerationError, "does not match the current profile"):
            _parse_review(
                {
                    "passed": False,
                    "species_accuracy": 5,
                    "anatomy_accuracy": 5,
                    "text_accuracy": 3,
                    "composition_quality": 5,
                    "location_free": True,
                    "findings": ["A stale length conflict"],
                    "correction_findings": [],
                    "profile_conflicts": [
                        {
                            "field": "measurements.length",
                            "profile_value": "10-20 cm",
                            "observed_value": "12-15 cm",
                            "sources": [
                                {"title": "Birds", "url": "https://birds.example/length"},
                                {"title": "Field", "url": "https://field.example/length"},
                            ],
                        }
                    ],
                    "verification_sources": [
                        {"title": "Birds", "url": "https://birds.example/length"},
                        {"title": "Field", "url": "https://field.example/length"},
                    ],
                },
                ("birds.example", "field.example"),
            )

    def test_field_mark_conflict_accepts_equivalent_json_and_canonicalizes_it(self) -> None:
        review = _parse_review(
            {
                "passed": False,
                "species_accuracy": 3,
                "anatomy_accuracy": 5,
                "text_accuracy": 5,
                "composition_quality": 5,
                "location_free": True,
                "findings": ["The proposed field marks conflict with direct sources"],
                "correction_findings": [],
                "profile_conflicts": [
                    {
                        "field": "field_marks",
                        "profile_value": json.dumps(_profile()["field_marks"]),
                        "observed_value": '["one","two","three","different"]',
                        "sources": [
                            {"title": "Birds", "url": "https://birds.example/marks"},
                            {"title": "Field", "url": "https://field.example/marks"},
                        ],
                    }
                ],
                "verification_sources": [
                    {"title": "Birds", "url": "https://birds.example/marks"},
                    {"title": "Field", "url": "https://field.example/marks"},
                ],
            },
            ("birds.example", "field.example"),
        )

        self.assertEqual(
            review.profile_conflicts[0]["profile_value"],
            '["one","two","three","four"]',
        )
        self.assertEqual(
            review.profile_conflicts[0]["observed_value"],
            '["one","two","three","different"]',
        )

    def test_field_mark_conflict_rejects_semantically_equal_observed_value(self) -> None:
        with self.assertRaisesRegex(GenerationError, "values must disagree"):
            _parse_review(
                {
                    "passed": False,
                    "species_accuracy": 3,
                    "anatomy_accuracy": 5,
                    "text_accuracy": 5,
                    "composition_quality": 5,
                    "location_free": True,
                    "findings": ["The proposed field marks conflict with direct sources"],
                    "correction_findings": [],
                    "profile_conflicts": [
                        {
                            "field": "field_marks",
                            "profile_value": '["one","two","three","four"]',
                            "observed_value": '["one", "two", "three", "four"]',
                            "sources": [
                                {"title": "Birds", "url": "https://birds.example/marks"},
                                {"title": "Field", "url": "https://field.example/marks"},
                            ],
                        }
                    ],
                    "verification_sources": [
                        {"title": "Birds", "url": "https://birds.example/marks"},
                        {"title": "Field", "url": "https://field.example/marks"},
                    ],
                },
                ("birds.example", "field.example"),
            )

    def test_field_mark_conflict_rejects_empty_observed_value(self) -> None:
        with self.assertRaisesRegex(GenerationError, "at least four strings"):
            _parse_review(
                {
                    "passed": False,
                    "species_accuracy": 3,
                    "anatomy_accuracy": 5,
                    "text_accuracy": 5,
                    "composition_quality": 5,
                    "location_free": True,
                    "findings": ["The proposed field marks conflict with direct sources"],
                    "correction_findings": [],
                    "profile_conflicts": [
                        {
                            "field": "field_marks",
                            "profile_value": '["one","two","three","four"]',
                            "observed_value": "[]",
                            "sources": [
                                {"title": "Birds", "url": "https://birds.example/marks"},
                                {"title": "Field", "url": "https://field.example/marks"},
                            ],
                        }
                    ],
                    "verification_sources": [
                        {"title": "Birds", "url": "https://birds.example/marks"},
                        {"title": "Field", "url": "https://field.example/marks"},
                    ],
                },
                ("birds.example", "field.example"),
            )

    def test_review_accepts_only_explicit_resolutions_from_history(self) -> None:
        review = _parse_review(
            {
                "passed": True,
                "species_accuracy": 5,
                "anatomy_accuracy": 5,
                "text_accuracy": 5,
                "composition_quality": 5,
                "location_free": True,
                "findings": ["The bill correction is now satisfied"],
                "correction_findings": [],
                "resolved_corrections": ["Shorten the bill"],
                "verification_sources": [
                    {"title": "Birds", "url": "https://birds.example/bill"},
                    {"title": "Field", "url": "https://field.example/bill"},
                ],
            },
            ("birds.example", "field.example"),
            prior_corrections=("Shorten the bill",),
        )

        self.assertEqual(review.resolved_corrections, ("Shorten the bill",))

        with self.assertRaisesRegex(GenerationError, "was not present in review history"):
            _parse_review(
                {
                    "passed": True,
                    "species_accuracy": 5,
                    "anatomy_accuracy": 5,
                    "text_accuracy": 5,
                    "composition_quality": 5,
                    "location_free": True,
                    "findings": [],
                    "correction_findings": [],
                    "resolved_corrections": ["Shorten the bill"],
                    "verification_sources": [
                        {"title": "Birds", "url": "https://birds.example/bill"},
                        {"title": "Field", "url": "https://field.example/bill"},
                    ],
                },
                ("birds.example", "field.example"),
            )

    def test_review_rejects_a_correction_as_resolved_and_actionable(self) -> None:
        with self.assertRaisesRegex(GenerationError, "both resolved and actionable"):
            _parse_review(
                {
                    "passed": False,
                    "species_accuracy": 3,
                    "anatomy_accuracy": 5,
                    "text_accuracy": 5,
                    "composition_quality": 5,
                    "location_free": True,
                    "findings": ["The bill remains too long"],
                    "correction_findings": ["Shorten the bill"],
                    "resolved_corrections": ["Shorten the bill"],
                    "verification_sources": [
                        {"title": "Birds", "url": "https://birds.example/bill"},
                        {"title": "Field", "url": "https://field.example/bill"},
                    ],
                },
                ("birds.example", "field.example"),
                prior_corrections=("Shorten the bill",),
            )

    def test_passing_review_shape_omits_empty_profile_conflicts(self) -> None:
        review = QualityReview(True, 5, 5, 5, 5, True, ())

        self.assertNotIn("profile_conflicts", review.as_dict())


if __name__ == "__main__":
    unittest.main()
