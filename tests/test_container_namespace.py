"""A repository rename must not strand existing container installations."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/container.yml"
SOURCE_SHA = "a" * 40


@pytest.mark.parametrize("repository", ["veteranbv/inky-bird-frame", "veteranbv/inky-bird"])
@pytest.mark.parametrize("event", ["workflow_run", "release", "workflow_dispatch"])
def test_image_tags_keep_the_compatibility_namespace(
    tmp_path: Path, repository: str, event: str
) -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "CONTROLLER_IMAGE: ghcr.io/${{ github.repository_owner }}/inky-bird\n" in workflow
    assert "COMPATIBILITY_IMAGE: ghcr.io/${{ github.repository_owner }}/inky-bird-frame" in workflow
    step = workflow.split("      - name: Validate image tag\n", 1)[1]
    step = step.split("\n      - name:", 1)[0]
    script = dedent(step.split("        run: |\n", 1)[1])

    # No network or repository mutation: only the tag-producing shell is run.
    fake_git = tmp_path / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  fetch) exit 0 ;;\n"
        f"  rev-parse) printf '%s\\n' '{SOURCE_SHA}' ;;\n"
        "  show) printf '%s\\n' '2026-01-01T00:00:00Z' ;;\n"
        "  *) exit 90 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    output = tmp_path / "github-output"
    result = subprocess.run(
        ["bash", "-eu", "-c", script],
        cwd=tmp_path,
        env={
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "EVENT_NAME": event,
            "GITHUB_REPOSITORY": repository,
            "REPOSITORY": repository,
            "CONTROLLER_IMAGE": "ghcr.io/veteranbv/inky-bird",
            "COMPATIBILITY_IMAGE": "ghcr.io/veteranbv/inky-bird-frame",
            "REQUESTED_TAG": "preview",
            "GITHUB_REF_NAME": "v1.2.3",
            "SOURCE_SHA": SOURCE_SHA,
            "GITHUB_OUTPUT": str(output),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = output.read_text(encoding="utf-8").splitlines()
    tags = lines[lines.index("tags<<EOF") + 1 : lines.index("EOF")]
    expected = {
        "workflow_run": [f"sha-{SOURCE_SHA}", "latest"],
        "release": ["1.2.3"],
        "workflow_dispatch": ["preview"],
    }
    assert tags == [
        f"ghcr.io/veteranbv/{image}:{tag}"
        for tag in expected[event]
        for image in ("inky-bird", "inky-bird-frame")
    ]


def test_release_bundle_uses_the_same_image_but_current_repository_links() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    bundle = workflow.split("      - name: Build Docker deployment bundle\n", 1)[1]
    assert "INKY_BIRD_IMAGE=${CONTROLLER_IMAGE}:${VERSION}" in bundle
    assert "https://github.com/${REPOSITORY}/blob/v${VERSION}/docs/docker.md" in bundle
    assert 'image="ghcr.io/${REPOSITORY}"' not in workflow
    assert "INKY_BIRD_IMAGE=ghcr.io/${REPOSITORY}" not in workflow
    # This compatibility change does not introduce an unmerged publication path.
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "github.event.workflow_run.event == 'push'" in workflow
    assert "github.actor == github.repository_owner" in workflow


def test_latest_release_download_keeps_the_supported_asset_name() -> None:
    guide = (ROOT / "docs/docker.md").read_text()
    workflow = WORKFLOW.read_text()
    assert "releases/latest/download/inky-bird-frame-docker.tar.gz" in guide
    assert "releases/latest/download/inky-bird-docker.tar.gz" not in guide
    assert "inky-bird-frame-docker.tar.gz.sha256" in workflow
    assert 'cp "${bundle}.tar.gz" inky-bird-frame-docker.tar.gz' in workflow


@pytest.mark.parametrize("mismatch", [False, True])
def test_published_names_must_resolve_to_one_multiarch_attested_digest(
    tmp_path: Path, mismatch: bool
) -> None:
    import json

    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert workflow.count("uses: docker/build-push-action@") == 1
    assert "provenance: mode=max" in workflow
    assert "sbom: true" in workflow
    step = workflow.split("      - name: Verify both published names\n", 1)[1].split(
        "\n  smoke-arm64:", 1
    )[0]
    script = dedent(step.split("        run: |\n", 1)[1])
    expected = "sha256:" + "a" * 64
    manifest = {
        "digest": expected,
        "manifests": [
            {"platform": {"os": "linux", "architecture": arch}} for arch in ("amd64", "arm64")
        ]
        + [
            {
                "platform": {"os": "unknown", "architecture": "unknown"},
                "annotations": {"vnd.docker.reference.type": "attestation-manifest"},
            }
        ]
        * 2,
    }
    good = json.dumps(manifest)
    manifest["digest"] = "sha256:" + "b" * 64
    bad = json.dumps(manifest)
    (tmp_path / "docker").write_text(
        "#!/bin/sh\ncase \"$4\" in\n *inky-bird-frame:*) printf '%s\\n' '"
        + (bad if mismatch else good)
        + "';;\n *) printf '%s\\n' '"
        + good
        + "';;\nesac\n"
    )
    (tmp_path / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for name in ("docker", "sleep"):
        (tmp_path / name).chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env={
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "EXPECTED_DIGEST": expected,
            "IMAGE_TAGS": "\n".join(
                (
                    "ghcr.io/veteranbv/inky-bird:latest",
                    "ghcr.io/veteranbv/inky-bird-frame:latest",
                )
            ),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode != 0) is mismatch, result.stderr


def _step_script(job: str, name: str) -> str:
    """Return the `run:` script of one named step of one job in container.yml."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    body = workflow.split(f"\n  {job}:\n", 1)[1]
    body = re.split(r"\n  [a-z][a-z0-9-]*:\n", body, maxsplit=1)[0]
    step = body.split(f"      - name: {name}\n", 1)[1].split("\n      - name:", 1)[0]
    return dedent(step.split("        run: |\n", 1)[1])


def _executable(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _run(
    script: str, cwd: Path, fakes: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    # Fakes first, then this interpreter (tomllib), then the system tools.
    path = os.pathsep.join((str(fakes), str(Path(sys.executable).parent), os.environ["PATH"]))
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        env={"PATH": path, **env},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("tag", "accepted"),
    [
        ("preview", True),
        ("latest", True),
        ("1.0.0", False),
        ("v1.0.0", False),
        ("V2", False),
        ("sha-" + SOURCE_SHA, False),
        ("SHA-anything", False),
    ],
)
def test_dispatch_cannot_claim_release_or_commit_tags(
    tmp_path: Path, tag: str, accepted: bool
) -> None:
    script = _step_script("publish", "Validate image tag")
    _executable(tmp_path, "git", "printf '%s\\n' '2026-01-01T00:00:00Z'\n")
    output = tmp_path / "github-output"
    result = _run(
        script,
        tmp_path,
        tmp_path,
        {
            "EVENT_NAME": "workflow_dispatch",
            "CONTROLLER_IMAGE": "ghcr.io/veteranbv/inky-bird",
            "COMPATIBILITY_IMAGE": "ghcr.io/veteranbv/inky-bird-frame",
            "REQUESTED_TAG": tag,
            "SOURCE_SHA": SOURCE_SHA,
            "GITHUB_OUTPUT": str(output),
        },
    )
    assert (result.returncode == 0) is accepted, result.stderr


def test_release_verifies_the_tested_main_images_before_promotion(tmp_path: Path) -> None:
    script = _step_script("publish", "Validate image tag")
    _executable(tmp_path, "git", "printf '%s\\n' '2026-01-01T00:00:00Z'\n")
    output = tmp_path / "github-output"
    result = _run(
        script,
        tmp_path,
        tmp_path,
        {
            "EVENT_NAME": "release",
            "CONTROLLER_IMAGE": "ghcr.io/veteranbv/inky-bird",
            "COMPATIBILITY_IMAGE": "ghcr.io/veteranbv/inky-bird-frame",
            "GITHUB_REF_NAME": "v1.2.3",
            "SOURCE_SHA": SOURCE_SHA,
            "GITHUB_OUTPUT": str(output),
        },
    )
    assert result.returncode == 0, result.stderr
    lines = output.read_text(encoding="utf-8").splitlines()
    verify = lines[lines.index("verify_tags<<EOF") + 1 :]
    verify = verify[: verify.index("EOF")]
    assert verify == [
        f"ghcr.io/veteranbv/inky-bird:sha-{SOURCE_SHA}",
        f"ghcr.io/veteranbv/inky-bird-frame:sha-{SOURCE_SHA}",
    ]
    assert "version=1.2.3" in lines


CI_RUNS_URL = (
    "repos/veteranbv/inky-bird/actions/workflows/ci.yml/runs"
    f"?head_sha={SOURCE_SHA}&event=push&branch=main&status=success&per_page=100"
)
CI_RUNS = {
    "workflow_runs": [
        {"head_sha": SOURCE_SHA, "conclusion": "success", "event": "push", "head_branch": "main"}
    ]
}


@pytest.mark.parametrize(
    ("ref_name", "head", "ancestor", "runs", "message"),
    [
        ("v1.2.3", SOURCE_SHA, True, CI_RUNS, None),
        ("v1.2.4", SOURCE_SHA, True, CI_RUNS, "does not match project version 1.2.3"),
        ("v1.2.3", "b" * 40, True, CI_RUNS, "is not the release commit"),
        ("v1.2.3", SOURCE_SHA, False, CI_RUNS, "is not on main"),
        ("v1.2.3", SOURCE_SHA, True, {"workflow_runs": []}, "No successful ci run"),
        (
            "v1.2.3",
            SOURCE_SHA,
            True,
            {"workflow_runs": [{**CI_RUNS["workflow_runs"][0], "conclusion": "failure"}]},
            "No successful ci run",
        ),
    ],
)
def test_release_requires_version_main_ancestry_and_passing_ci(
    tmp_path: Path,
    ref_name: str,
    head: str,
    ancestor: bool,
    runs: dict[str, list[dict[str, str]]],
    message: str | None,
) -> None:
    import json

    script = _step_script("publish", "Verify release source")
    work = tmp_path / "work"
    fakes = tmp_path / "bin"
    work.mkdir()
    fakes.mkdir()
    (work / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.2.3"\n')
    (tmp_path / "runs.json").write_text(json.dumps(runs))
    _executable(
        fakes,
        "git",
        'case "$1" in\n'
        f"  rev-parse) printf '%s\\n' '{head}' ;;\n"
        "  fetch) exit 0 ;;\n"
        f"  merge-base) exit {0 if ancestor else 1} ;;\n"
        "  *) exit 90 ;;\n"
        "esac\n",
    )
    # Applies the workflow's own --jq filter to a recorded API response.
    _executable(
        fakes,
        "gh",
        'test "$1" = api || exit 90\n'
        f'test "$2" = "{CI_RUNS_URL}" || exit 91\n'
        'test "$3" = --jq || exit 92\n'
        f'exec jq -r "$4" "{tmp_path / "runs.json"}"\n',
    )
    result = _run(
        script,
        work,
        fakes,
        {
            "GITHUB_REF_NAME": ref_name,
            "GITHUB_REPOSITORY": "veteranbv/inky-bird",
            "SOURCE_SHA": SOURCE_SHA,
        },
    )
    if message is None:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0
        assert message in result.stdout + result.stderr


DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64


@pytest.mark.parametrize(
    ("existing", "inspect_error", "created", "succeeds"),
    [
        (None, "ERROR: ghcr.io/veteranbv/inky-bird:1.2.3: not found", True, True),
        (DIGEST, "", False, True),
        (OTHER_DIGEST, "", False, False),
        (None, "ERROR: unexpected status from HEAD request: 503", False, False),
    ],
)
def test_release_promotion_never_moves_a_published_version(
    tmp_path: Path, existing: str | None, inspect_error: str, created: bool, succeeds: bool
) -> None:
    script = _step_script("promote-release", "Tag the tested digest")
    log = tmp_path / "docker.log"
    state = tmp_path / "created"
    state.mkdir()
    # inspect: answers from the scenario until this fake has created the tag.
    _executable(
        tmp_path,
        "docker",
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'test "$1 $2" = "buildx imagetools" || exit 90\n'
        'case "$3" in\n'
        "  inspect)\n"
        f'    if [ -e "{state}/$(printf %s "$4" | tr "/:" "__")" ]; then\n'
        f"      printf '%s\\n' '{{\"digest\": \"{DIGEST}\"}}'; exit 0\n"
        "    fi\n"
        + (
            f"    printf '%s\\n' '{{\"digest\": \"{existing}\"}}'; exit 0 ;;\n"
            if existing
            else f"    printf '%s\\n' '{inspect_error}' >&2; exit 1 ;;\n"
        )
        + "  create)\n"
        f'    touch "{state}/$(printf %s "$5" | tr "/:" "__")"; exit 0 ;;\n'
        "  *) exit 91 ;;\n"
        "esac\n",
    )
    _executable(tmp_path, "sleep", "exit 0\n")
    result = _run(
        script,
        tmp_path,
        tmp_path,
        {
            "CONTROLLER_IMAGE": "ghcr.io/veteranbv/inky-bird",
            "COMPATIBILITY_IMAGE": "ghcr.io/veteranbv/inky-bird-frame",
            "DIGEST": DIGEST,
            "VERSION": "1.2.3",
        },
    )
    assert (result.returncode == 0) is succeeds, result.stdout + result.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    creates = [call for call in calls if call.startswith("buildx imagetools create")]
    if created:
        assert creates == [
            f"buildx imagetools create --tag ghcr.io/veteranbv/{name}:1.2.3 "
            f"ghcr.io/veteranbv/{name}@{DIGEST}"
            for name in ("inky-bird", "inky-bird-frame")
        ]
    else:
        assert creates == []


@pytest.mark.parametrize(("reported", "succeeds"), [("1.2.3", True), ("1.2.3rc1", False)])
def test_arm64_smoke_requires_the_release_version(
    tmp_path: Path, reported: str, succeeds: bool
) -> None:
    script = _step_script("smoke-arm64", "Run the published image natively")
    log = tmp_path / "docker.log"
    _executable(
        tmp_path,
        "docker",
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'case "$1" in\n'
        "  pull) exit 0 ;;\n"
        "  image) printf '%s\\n' linux/arm64 ;;\n"
        '  run) case "$*" in *" --version") '
        f"printf '%s\\n' 'inky-bird {reported}' ;; esac ;;\n"
        "  *) exit 90 ;;\n"
        "esac\n",
    )
    result = _run(
        script,
        tmp_path,
        tmp_path,
        {"CONTROLLER_IMAGE": "ghcr.io/veteranbv/inky-bird", "DIGEST": DIGEST, "VERSION": "1.2.3"},
    )
    assert (result.returncode == 0) is succeeds, result.stdout + result.stderr
    calls = log.read_text(encoding="utf-8")
    if succeeds:
        assert (
            f"--platform linux/arm64 ghcr.io/veteranbv/inky-bird@{DIGEST} catalog validate" in calls
        )


def test_release_promotes_instead_of_rebuilding_and_keeps_assets_immutable() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    build = workflow.split("      - name: Build and publish\n", 1)[1].split("\n      - name:", 1)[0]
    assert "if: github.event_name != 'release'" in build
    assert "runs-on: ubuntu-24.04-arm" in workflow.split("\n  smoke-arm64:\n", 1)[1]
    promote = workflow.split("\n  promote-release:\n", 1)[1].split("\n  release-bundle:\n", 1)[0]
    assert "if: github.event_name == 'release'" in promote
    assert "- smoke-arm64" in promote
    bundle = workflow.split("\n  release-bundle:\n", 1)[1]
    assert "- promote-release" in bundle
    assert "--clobber" not in workflow
