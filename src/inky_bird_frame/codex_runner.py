"""Subscription-backed Codex execution for research, image generation, and QA."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import IO, Final, cast
from urllib.parse import urlsplit

from .birds import BirdSpecies, TaxonContext
from .errors import GenerationError
from .http import write_bytes_atomic
from .models import (
    PROFILE_CONFLICT_FIELDS,
    ProfileConflict,
    QualityReview,
    ReferencePhoto,
    SourceLink,
    SpeciesProfileData,
)
from .prompts import plate_prompt, profile_prompt, review_prompt

PROFILE_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "properties": {
        "taxon_id": {"type": "integer"},
        "common_name": {"type": "string"},
        "scientific_name": {"type": "string"},
        "family": {"type": "string"},
        "measurements": {
            "type": "object",
            "properties": {
                "length": {"type": "string"},
                "wingspan": {"type": "string"},
                "weight": {"type": "string"},
            },
            "required": ["length", "wingspan", "weight"],
            "additionalProperties": False,
        },
        "field_marks": {"type": "array", "items": {"type": "string"}},
        "habitat": {"type": "string"},
        "behavior": {"type": "string"},
        "palette": {"type": "array", "items": {"type": "string"}},
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
                "required": ["title", "url"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "taxon_id",
        "common_name",
        "scientific_name",
        "family",
        "measurements",
        "field_marks",
        "habitat",
        "behavior",
        "palette",
        "sources",
    ],
    "additionalProperties": False,
}

REVIEW_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "species_accuracy": {"type": "integer", "minimum": 1, "maximum": 5},
        "anatomy_accuracy": {"type": "integer", "minimum": 1, "maximum": 5},
        "text_accuracy": {"type": "integer", "minimum": 1, "maximum": 5},
        "composition_quality": {"type": "integer", "minimum": 1, "maximum": 5},
        "location_free": {"type": "boolean"},
        "findings": {"type": "array", "items": {"type": "string"}},
        "correction_findings": {"type": "array", "items": {"type": "string"}},
        "resolved_corrections": {"type": "array", "items": {"type": "string"}},
        "profile_conflicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": list(PROFILE_CONFLICT_FIELDS)},
                    "profile_value": {"type": "string"},
                    "observed_value": {"type": "string"},
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "url": {"type": "string"},
                            },
                            "required": ["title", "url"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["field", "profile_value", "observed_value", "sources"],
                "additionalProperties": False,
            },
        },
        "verification_sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
                "required": ["title", "url"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "passed",
        "species_accuracy",
        "anatomy_accuracy",
        "text_accuracy",
        "composition_quality",
        "location_free",
        "findings",
        "correction_findings",
        "resolved_corrections",
        "profile_conflicts",
        "verification_sources",
    ],
    "additionalProperties": False,
}


TERMINATE_GRACE_SECONDS: Final = 5.0
MAX_CAPTURED_OUTPUT_BYTES: Final = 4 * 1024 * 1024

# The agent reads third-party web pages, so it gets only what the Codex CLI and
# its network stack need, never the job's provider keys, notification URLs or
# OIDC secrets. Codex authenticates with its own ChatGPT login under CODEX_HOME
# (default ~/.codex); API-key variables are deliberately not passed.
# - PATH, HOME, USER, LOGNAME, SHELL: executable lookup, login and tool shells.
# - LANG, LANGUAGE, LC_*, TZ: text encoding and local time.
# - TMPDIR, TMP, TEMP: scratch files.
# - SSL_CERT_FILE, SSL_CERT_DIR, NODE_EXTRA_CA_CERTS, CODEX_CA_CERTIFICATE:
#   operator TLS trust for the native binary and its Node launcher.
# - HTTP(S)_PROXY, ALL_PROXY, NO_PROXY and lowercase forms: outbound proxies.
# - CODEX_HOME, XDG_*: Codex configuration, credentials and caches.
# - DBUS_SESSION_BUS_ADDRESS: a keyring-backed Codex credential store on Linux.
CHILD_ENVIRONMENT_NAMES: Final = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LANGUAGE",
        "TZ",
        "TMPDIR",
        "TMP",
        "TEMP",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
        "CODEX_CA_CERTIFICATE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "CODEX_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
    }
)
CHILD_ENVIRONMENT_PREFIXES: Final = ("LC_",)


def child_environment() -> dict[str, str]:
    """Return the allowlisted subset of this process's environment for Codex."""
    return {
        name: value
        for name, value in os.environ.items()
        if name in CHILD_ENVIRONMENT_NAMES or name.startswith(CHILD_ENVIRONMENT_PREFIXES)
    }


class _BoundedCapture(threading.Thread):
    """Drain one pipe to EOF, keeping at most MAX_CAPTURED_OUTPUT_BYTES."""

    def __init__(self, stream: IO[bytes]) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._chunks: list[bytes] = []
        self._kept = 0
        self._dropped = 0

    def run(self) -> None:
        limit = MAX_CAPTURED_OUTPUT_BYTES
        try:
            while chunk := os.read(self._stream.fileno(), 65536):
                room = max(limit - self._kept, 0)
                if room:
                    self._chunks.append(chunk[:room])
                    self._kept += min(room, len(chunk))
                self._dropped += max(len(chunk) - room, 0)
        except (OSError, ValueError):
            pass
        finally:
            self._stream.close()

    def text(self) -> str:
        text = b"".join(self._chunks).decode("utf-8", errors="replace")
        if self._dropped:
            text += f"\n[output truncated: {self._dropped} more bytes not kept]"
        return text


def _write_prompt(stream: IO[bytes], prompt: bytes) -> None:
    try:
        stream.write(prompt)
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        with contextlib.suppress(BrokenPipeError, OSError):
            stream.close()


def _signal_group(group: int, stop_signal: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(group, stop_signal)


def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
    # The group ID cannot be reused while any member, including the unreaped
    # leader, remains; after the leader exits on SIGTERM, the final SIGKILL
    # still reaches members that ignored it.
    _signal_group(process.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=TERMINATE_GRACE_SECONDS)
    _signal_group(process.pid, signal.SIGKILL)
    process.wait()


class CodexRunner:
    def __init__(
        self,
        executable: Path,
        workspace: Path,
        timeout_seconds: int = 1200,
        *,
        model: str | None = None,
    ) -> None:
        self.executable = executable
        self.workspace = workspace.resolve()
        self.timeout_seconds = timeout_seconds
        self.model = model
        if not self.executable.is_file():
            raise GenerationError(f"Codex executable not found: {self.executable}")

    def _base_command(self, *, writable: bool, search: bool = False) -> list[str]:
        command = [str(self.executable)]
        if search:
            command.append("--search")
        command.extend(
            [
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "workspace-write" if writable else "read-only",
            ]
        )
        if self.model is not None:
            command.extend(["--model", self.model])
        return command

    def _run(
        self,
        command: list[str],
        prompt: str,
        log_path: Path,
    ) -> subprocess.CompletedProcess[str]:
        try:
            prompt_bytes = prompt.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise GenerationError("Codex prompt is not valid UTF-8 text") from exc
        try:
            # A new session makes the agent the leader of its own process group,
            # so a timeout can stop every tool process it started.
            process = subprocess.Popen(
                command,
                cwd=self.workspace,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=child_environment(),
                start_new_session=True,
            )
        except OSError as exc:
            raise GenerationError(f"Codex could not be started: {exc.strerror or exc}") from exc
        if process.stdin is None or process.stdout is None or process.stderr is None:
            _stop_process_group(process)
            raise GenerationError("Codex could not be started with its input and output pipes")
        stdout = _BoundedCapture(process.stdout)
        stderr = _BoundedCapture(process.stderr)
        writer = threading.Thread(
            target=_write_prompt, args=(process.stdin, prompt_bytes), daemon=True
        )
        for thread in (stdout, stderr, writer):
            thread.start()
        timed_out = False
        try:
            process.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _stop_process_group(process)
        except BaseException:
            _stop_process_group(process)
            raise
        # Tool processes that outlive the agent would otherwise hold its output
        # pipes open and keep running unsupervised.
        for stop_signal in (None, signal.SIGTERM, signal.SIGKILL):
            if stop_signal is not None:
                _signal_group(process.pid, stop_signal)
            stdout.join(TERMINATE_GRACE_SECONDS)
            stderr.join(TERMINATE_GRACE_SECONDS)
            if not stdout.is_alive() and not stderr.is_alive():
                break
        writer.join(TERMINATE_GRACE_SECONDS)
        stdout_text = stdout.text()
        stderr_text = stderr.text()
        status = "timed out" if timed_out else f"exit status {process.returncode}"
        try:
            write_bytes_atomic(
                log_path,
                (
                    f"COMMAND: {' '.join(command)}\n\nRESULT: {status}\n\n"
                    f"STDOUT:\n{stdout_text}\n\nSTDERR:\n{stderr_text}"
                ).encode("utf-8", errors="replace"),
            )
        except OSError as exc:
            raise GenerationError(f"Codex log could not be written: {log_path}") from exc
        if timed_out:
            raise GenerationError(
                f"Codex timed out after {self.timeout_seconds} seconds; see {log_path}"
            )
        if process.returncode != 0:
            raise GenerationError(f"Codex exited with status {process.returncode}; see {log_path}")
        return subprocess.CompletedProcess(command, process.returncode, stdout_text, stderr_text)

    def _structured(
        self,
        prompt: str,
        schema: dict[str, object],
        images: list[Path],
        output_path: Path,
        log_path: Path,
        *,
        search: bool,
    ) -> object:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            output_path.unlink(missing_ok=True)
        except OSError as exc:
            raise GenerationError(
                f"Previous Codex structured output could not be removed: {output_path}"
            ) from exc
        with TemporaryDirectory(dir=output_path.parent) as temporary:
            schema_path = Path(temporary) / "schema.json"
            schema_path.write_text(json.dumps(schema, indent=2, sort_keys=True))
            command = self._base_command(writable=True, search=search)
            for image in images:
                command.extend(["--image", str(image.resolve())])
            command.extend(["--output-schema", str(schema_path), "-o", str(output_path), "-"])
            self._run(command, prompt, log_path)
        try:
            with output_path.open("rb") as handle:
                content = handle.read(MAX_STRUCTURED_OUTPUT_BYTES + 1)
            if len(content) > MAX_STRUCTURED_OUTPUT_BYTES:
                raise GenerationError(f"Codex structured output is too large: {output_path}")
            return cast(object, json.loads(content.decode("utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GenerationError(
                f"Codex did not write valid structured output: {output_path}"
            ) from exc

    def create_profile(
        self,
        species: BirdSpecies,
        context: TaxonContext,
        references: list[ReferencePhoto],
        reference_paths: list[Path],
        output_path: Path,
        log_path: Path,
        *,
        allowed_domains: tuple[str, ...],
        prior_profile: SpeciesProfileData | None = None,
        profile_conflicts: tuple[ProfileConflict, ...] = (),
    ) -> SpeciesProfileData:
        raw = self._structured(
            profile_prompt(
                species,
                context,
                references,
                allowed_domains,
                prior_profile=prior_profile,
                profile_conflicts=profile_conflicts,
            ),
            PROFILE_SCHEMA,
            reference_paths,
            output_path,
            log_path,
            search=True,
        )
        profile = parse_species_profile(raw, allowed_domains)
        if (
            profile["taxon_id"] != species.taxon_id
            or profile["common_name"] != species.common_name
            or profile["scientific_name"] != species.scientific_name
        ):
            raise GenerationError("Codex profile identity does not match the discovered taxon")
        return profile

    def generate_plate(
        self,
        species: BirdSpecies,
        profile: SpeciesProfileData,
        references: list[ReferencePhoto],
        reference_paths: list[Path],
        output_path: Path,
        log_path: Path,
        correction_findings: tuple[str, ...] = (),
        *,
        correction_source_path: Path | None = None,
        invariant_findings: tuple[str, ...] = (),
    ) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = self._base_command(writable=True)
        if correction_source_path is not None:
            command.extend(["--image", str(correction_source_path.resolve())])
        for image in reference_paths:
            command.extend(["--image", str(image.resolve())])
        command.extend(["-"])
        self._run(
            command,
            plate_prompt(
                species,
                profile,
                references,
                output_path,
                correction_findings,
                invariant_findings=invariant_findings,
                has_correction_source=correction_source_path is not None,
            ),
            log_path,
        )
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise GenerationError(f"Codex did not create the requested plate: {output_path}")
        return output_path

    def review_plate(
        self,
        species: BirdSpecies,
        profile: SpeciesProfileData,
        references: list[ReferencePhoto],
        plate_path: Path,
        reference_paths: list[Path],
        output_path: Path,
        log_path: Path,
        *,
        allowed_domains: tuple[str, ...],
        prior_corrections: tuple[str, ...] = (),
        prior_profile_conflicts: tuple[ProfileConflict, ...] = (),
    ) -> QualityReview:
        raw = self._structured(
            review_prompt(
                species,
                profile,
                references,
                allowed_domains,
                prior_corrections=prior_corrections,
                prior_profile_conflicts=prior_profile_conflicts,
            ),
            REVIEW_SCHEMA,
            [plate_path, *reference_paths],
            output_path,
            log_path,
            search=True,
        )
        return _parse_review(
            raw,
            profile,
            allowed_domains,
            prior_corrections=prior_corrections,
        )


# Generous ceilings, roughly ten times the largest approved catalog value, so a
# runaway model output cannot reach the manifest, later prompts or the catalog.
MAX_STRUCTURED_OUTPUT_BYTES: Final = 1024 * 1024
MAX_NAME_LENGTH: Final = 200
MAX_MEASUREMENT_LENGTH: Final = 500
MAX_PROSE_LENGTH: Final = 4000
MAX_FIELD_MARK_LENGTH: Final = 1000
MAX_PALETTE_LENGTH: Final = 300
MAX_SOURCE_TITLE_LENGTH: Final = 500
MAX_SOURCE_URL_LENGTH: Final = 2048
MAX_PROFILE_LIST_ITEMS: Final = 32
MAX_SOURCES: Final = 32
MAX_FINDINGS: Final = 100
MAX_CORRECTIONS: Final = 50
MAX_CONFLICT_VALUE_LENGTH: Final = 40000


def _non_empty_string(value: object, field: str, max_length: int = MAX_PROSE_LENGTH) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GenerationError(f"Codex output field {field} must be a non-empty string")
    text = value.strip()
    if len(text) > max_length:
        raise GenerationError(
            f"Codex output field {field} must contain at most {max_length} characters"
        )
    return text


def _bounded_list(value: object, field: str, max_items: int) -> list[object]:
    if not isinstance(value, list):
        raise GenerationError(f"Codex output field {field} must be a list")
    if len(value) > max_items:
        raise GenerationError(f"Codex output field {field} must contain at most {max_items} items")
    return cast(list[object], value)


def _string_list(
    value: object,
    field: str,
    minimum: int,
    *,
    max_items: int,
    max_length: int = MAX_PROSE_LENGTH,
) -> list[str]:
    items = [
        _non_empty_string(item, field, max_length)
        for item in _bounded_list(value, field, max_items)
    ]
    if len(items) < minimum:
        raise GenerationError(f"Codex output field {field} must contain at least {minimum} items")
    return items


def _source_hostname(url: str) -> str:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
        if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
            raise ValueError("invalid source")
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("invalid port")
    except ValueError as exc:
        raise GenerationError("Codex source URL is invalid") from exc
    return hostname


def _allowed_source(url: str, allowed_domains: tuple[str, ...] | None) -> bool:
    hostname = _source_hostname(url)
    if allowed_domains is None:
        return True
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in allowed_domains)


def _source_identity(url: str, allowed_domains: tuple[str, ...] | None) -> str | None:
    hostname = _source_hostname(url)
    if allowed_domains is None:
        return hostname
    return next(
        (
            domain
            for domain in allowed_domains
            if hostname == domain or hostname.endswith(f".{domain}")
        ),
        None,
    )


def parse_species_profile(
    raw: object, allowed_domains: tuple[str, ...] | None = None
) -> SpeciesProfileData:
    if not isinstance(raw, dict):
        raise GenerationError("Codex profile output must be an object")
    taxon_id = raw.get("taxon_id")
    measurements = raw.get("measurements")
    sources = raw.get("sources")
    if not isinstance(taxon_id, int) or not isinstance(measurements, dict):
        raise GenerationError("Codex profile has invalid identity or measurements")
    if not isinstance(sources, list):
        raise GenerationError("Codex profile sources must be a list")
    parsed_sources: list[SourceLink] = []
    source_hosts: set[str] = set()
    for source in _bounded_list(sources, "sources", MAX_SOURCES):
        if not isinstance(source, dict):
            raise GenerationError("Codex profile source must be an object")
        url = _non_empty_string(source.get("url"), "sources.url", MAX_SOURCE_URL_LENGTH)
        if not url.startswith("https://"):
            raise GenerationError("Codex profile source URLs must use HTTPS")
        if not _allowed_source(url, allowed_domains):
            raise GenerationError("Codex profile cited a source outside the configured allowlist")
        source_identity = _source_identity(url, allowed_domains)
        if source_identity is not None:
            source_hosts.add(source_identity)
        parsed_sources.append(
            SourceLink(
                title=_non_empty_string(
                    source.get("title"), "sources.title", MAX_SOURCE_TITLE_LENGTH
                ),
                url=url,
            )
        )
    if len(parsed_sources) < 2:
        raise GenerationError("Codex profile must cite at least two sources")
    if len(source_hosts) < 2:
        raise GenerationError("Codex profile must cite at least two independent source domains")
    return SpeciesProfileData(
        taxon_id=taxon_id,
        common_name=_non_empty_string(raw.get("common_name"), "common_name", MAX_NAME_LENGTH),
        scientific_name=_non_empty_string(
            raw.get("scientific_name"), "scientific_name", MAX_NAME_LENGTH
        ),
        family=_non_empty_string(raw.get("family"), "family", MAX_NAME_LENGTH),
        measurements={
            "length": _non_empty_string(
                measurements.get("length"), "measurements.length", MAX_MEASUREMENT_LENGTH
            ),
            "wingspan": _non_empty_string(
                measurements.get("wingspan"), "measurements.wingspan", MAX_MEASUREMENT_LENGTH
            ),
            "weight": _non_empty_string(
                measurements.get("weight"), "measurements.weight", MAX_MEASUREMENT_LENGTH
            ),
        },
        field_marks=_string_list(
            raw.get("field_marks"),
            "field_marks",
            4,
            max_items=MAX_PROFILE_LIST_ITEMS,
            max_length=MAX_FIELD_MARK_LENGTH,
        ),
        habitat=_non_empty_string(raw.get("habitat"), "habitat"),
        behavior=_non_empty_string(raw.get("behavior"), "behavior"),
        palette=_string_list(
            raw.get("palette"),
            "palette",
            3,
            max_items=MAX_PROFILE_LIST_ITEMS,
            max_length=MAX_PALETTE_LENGTH,
        ),
        sources=parsed_sources,
    )


def _score(raw: dict[str, object], field: str) -> int:
    value = raw.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 5:
        raise GenerationError(f"Codex review field {field} must be an integer from 1 to 5")
    return value


def _parse_profile_conflicts(
    value: object,
    current_profile: SpeciesProfileData,
    allowed_domains: tuple[str, ...] | None,
) -> tuple[ProfileConflict, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise GenerationError("Codex review field profile_conflicts must be a list")
    conflicts: list[ProfileConflict] = []
    fields: set[str] = set()
    for raw_conflict in _bounded_list(value, "profile_conflicts", len(PROFILE_CONFLICT_FIELDS)):
        if not isinstance(raw_conflict, dict):
            raise GenerationError("Codex profile conflict must be an object")
        field = _non_empty_string(raw_conflict.get("field"), "profile_conflicts.field")
        if field not in PROFILE_CONFLICT_FIELDS:
            raise GenerationError(f"Codex profile conflict field is not supported: {field}")
        if field in fields:
            raise GenerationError(f"Codex profile conflict field is duplicated: {field}")
        fields.add(field)
        profile_value = _non_empty_string(
            raw_conflict.get("profile_value"),
            "profile_conflicts.profile_value",
            MAX_CONFLICT_VALUE_LENGTH,
        )
        expected_profile_value: str
        if field == "measurements.length":
            expected_profile_value = current_profile["measurements"]["length"]
        elif field == "measurements.wingspan":
            expected_profile_value = current_profile["measurements"]["wingspan"]
        elif field == "measurements.weight":
            expected_profile_value = current_profile["measurements"]["weight"]
        elif field == "field_marks":
            expected_profile_value = json.dumps(
                current_profile["field_marks"],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        elif field == "family":
            expected_profile_value = current_profile["family"]
        elif field == "habitat":
            expected_profile_value = current_profile["habitat"]
        elif field == "behavior":
            expected_profile_value = current_profile["behavior"]
        else:
            raise GenerationError(f"Unsupported profile conflict field: {field}")
        matches_current_profile = profile_value == expected_profile_value
        if field == "field_marks":
            try:
                matches_current_profile = (
                    json.loads(profile_value) == current_profile["field_marks"]
                )
            except json.JSONDecodeError:
                matches_current_profile = False
        if not matches_current_profile:
            raise GenerationError(
                f"Codex profile conflict does not match the current profile field: {field}"
            )
        profile_value = expected_profile_value
        observed_value = _non_empty_string(
            raw_conflict.get("observed_value"),
            "profile_conflicts.observed_value",
            MAX_CONFLICT_VALUE_LENGTH if field == "field_marks" else MAX_PROSE_LENGTH,
        )
        if field == "field_marks":
            try:
                observed_field_marks = json.loads(observed_value)
            except json.JSONDecodeError as exc:
                raise GenerationError(
                    "Codex field_marks conflict observed_value must be a JSON array"
                ) from exc
            if (
                not isinstance(observed_field_marks, list)
                or len(observed_field_marks) < 4
                or any(
                    not isinstance(mark, str) or not mark.strip() for mark in observed_field_marks
                )
            ):
                raise GenerationError(
                    "Codex field_marks conflict observed_value must contain at least four strings"
                )
            _string_list(
                observed_field_marks,
                "profile_conflicts.observed_value",
                4,
                max_items=MAX_PROFILE_LIST_ITEMS,
                max_length=MAX_FIELD_MARK_LENGTH,
            )
            if observed_field_marks == current_profile["field_marks"]:
                raise GenerationError("Codex profile conflict values must disagree")
            observed_value = json.dumps(
                observed_field_marks,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        if profile_value == observed_value:
            raise GenerationError("Codex profile conflict values must disagree")
        raw_sources = raw_conflict.get("sources")
        if not isinstance(raw_sources, list):
            raise GenerationError("Codex profile conflict sources must be a list")
        sources: list[SourceLink] = []
        urls: set[str] = set()
        domains: set[str] = set()
        for raw_source in _bounded_list(raw_sources, "profile_conflicts.sources", MAX_SOURCES):
            if not isinstance(raw_source, dict):
                raise GenerationError("Codex profile conflict source must be an object")
            url = _non_empty_string(
                raw_source.get("url"), "profile_conflicts.sources.url", MAX_SOURCE_URL_LENGTH
            )
            if not url.startswith("https://"):
                raise GenerationError("Codex profile conflict source URLs must use HTTPS")
            if not _allowed_source(url, allowed_domains):
                raise GenerationError(
                    "Codex profile conflict cited a source outside the configured allowlist"
                )
            if url in urls:
                continue
            urls.add(url)
            identity = _source_identity(url, allowed_domains)
            if identity is not None:
                domains.add(identity)
            sources.append(
                SourceLink(
                    title=_non_empty_string(
                        raw_source.get("title"),
                        "profile_conflicts.sources.title",
                        MAX_SOURCE_TITLE_LENGTH,
                    ),
                    url=url,
                )
            )
        if len(sources) < 2 or len(domains) < 2:
            raise GenerationError(
                "Codex profile conflicts must cite two independent verification sources"
            )
        conflicts.append(
            ProfileConflict(
                field=field,
                profile_value=profile_value,
                observed_value=observed_value,
                sources=sources,
            )
        )
    return tuple(conflicts)


def _parse_resolved_corrections(
    value: object,
    prior_corrections: tuple[str, ...],
    current_corrections: tuple[str, ...],
) -> tuple[str, ...]:
    if value is None:
        return ()
    items = _string_list(
        value,
        "resolved_corrections",
        0,
        max_items=max(MAX_CORRECTIONS, len(prior_corrections)),
    )
    prior_by_key = {" ".join(item.split()).casefold(): item for item in prior_corrections}
    current_keys = {" ".join(item.split()).casefold() for item in current_corrections}
    resolved: list[str] = []
    resolved_keys: set[str] = set()
    for item in items:
        key = " ".join(item.split()).casefold()
        if key not in prior_by_key:
            raise GenerationError("Codex resolved correction was not present in review history")
        if key in current_keys:
            raise GenerationError("Codex correction cannot be both resolved and actionable")
        if key in resolved_keys:
            raise GenerationError("Codex resolved correction is duplicated")
        resolved_keys.add(key)
        resolved.append(prior_by_key[key])
    return tuple(resolved)


def _parse_review(
    raw: object,
    current_profile: SpeciesProfileData,
    allowed_domains: tuple[str, ...] | None = None,
    *,
    prior_corrections: tuple[str, ...] = (),
) -> QualityReview:
    if not isinstance(raw, dict):
        raise GenerationError("Codex review output must be an object")
    reported_pass = raw.get("passed") is True
    location_free = raw.get("location_free") is True
    species_accuracy = _score(raw, "species_accuracy")
    anatomy_accuracy = _score(raw, "anatomy_accuracy")
    text_accuracy = _score(raw, "text_accuracy")
    composition_quality = _score(raw, "composition_quality")
    findings = tuple(_string_list(raw.get("findings"), "findings", 0, max_items=MAX_FINDINGS))
    correction_findings = tuple(
        _string_list(
            raw.get("correction_findings"), "correction_findings", 0, max_items=MAX_CORRECTIONS
        )
    )
    resolved_corrections = _parse_resolved_corrections(
        raw.get("resolved_corrections"),
        prior_corrections,
        correction_findings,
    )
    profile_conflicts = _parse_profile_conflicts(
        raw.get("profile_conflicts"),
        current_profile,
        allowed_domains,
    )
    sources = raw.get("verification_sources")
    if not isinstance(sources, list):
        raise GenerationError("Codex review verification_sources must be a list")
    verification_sources: list[SourceLink] = []
    source_urls: set[str] = set()
    source_hosts: set[str] = set()
    for source in _bounded_list(sources, "verification_sources", MAX_SOURCES):
        if not isinstance(source, dict):
            raise GenerationError("Codex review verification source must be an object")
        url = _non_empty_string(
            source.get("url"), "verification_sources.url", MAX_SOURCE_URL_LENGTH
        )
        if not url.startswith("https://"):
            raise GenerationError("Codex review source URLs must use HTTPS")
        if not _allowed_source(url, allowed_domains):
            raise GenerationError("Codex review cited a source outside the configured allowlist")
        if url in source_urls:
            continue
        source_urls.add(url)
        source_identity = _source_identity(url, allowed_domains)
        if source_identity is not None:
            source_hosts.add(source_identity)
        verification_sources.append(
            SourceLink(
                title=_non_empty_string(
                    source.get("title"), "verification_sources.title", MAX_SOURCE_TITLE_LENGTH
                ),
                url=url,
            )
        )
    if len(verification_sources) < 2:
        raise GenerationError("Codex review must cite at least two verification sources")
    if len(source_hosts) < 2:
        raise GenerationError(
            "Codex review must cite at least two independent verification source domains"
        )
    requires_correction = not (
        reported_pass
        and location_free
        and min(
            species_accuracy,
            anatomy_accuracy,
            text_accuracy,
            composition_quality,
        )
        >= 4
    )
    if requires_correction and not (correction_findings or profile_conflicts):
        raise GenerationError(
            "Failed Codex reviews must include correction_findings or profile_conflicts"
        )
    if not requires_correction and (correction_findings or profile_conflicts):
        raise GenerationError(
            "Passing Codex reviews must not include correction_findings or profile_conflicts"
        )
    return QualityReview(
        passed=not requires_correction,
        species_accuracy=species_accuracy,
        anatomy_accuracy=anatomy_accuracy,
        text_accuracy=text_accuracy,
        composition_quality=composition_quality,
        location_free=location_free,
        findings=findings,
        verification_sources=tuple(verification_sources),
        correction_findings=correction_findings,
        profile_conflicts=profile_conflicts,
        resolved_corrections=resolved_corrections,
    )
