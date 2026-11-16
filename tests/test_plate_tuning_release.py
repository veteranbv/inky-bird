"""A release arm sends exactly what an earlier release sent, rendered by its own module."""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from pathlib import Path

import pytest
from plate_tuning.config import HarnessError
from plate_tuning.generate import (
    REPOSITORY,
    CodexGenerator,
    generate_samples,
    parse_arm,
    register_arm,
    sample_dir,
)
from plate_tuning.panel import frozen_references
from plate_tuning.rundir import Budget
from plate_tuning_fixtures import ColourGenerator, make_run
from test_prompts import (
    _FIXTURE_DOMAINS,
    RENDERED_PROMPT_SHA256,
    _fixture_conflict,
    _fixture_profile,
    _fixture_references,
    _fixture_species,
)

from inky_bird_frame.birds import TaxonContext

# The last commit on main that changed the prompt module while it was field-journal-v8.
V8_COMMIT = "bd2ee8caf12b530e1aaa14a102274ebdedd9fc83"


def _has_commit(commit: str) -> bool:
    found = subprocess.run(
        ["git", "-C", str(REPOSITORY), "cat-file", "-e", f"{commit}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    return found.returncode == 0


needs_v8 = pytest.mark.skipif(
    not _has_commit(V8_COMMIT), reason="this clone does not hold the v8 release commit"
)


@needs_v8
def test_a_release_arm_renders_v8_byte_for_byte() -> None:
    arm = parse_arm(f"v8=ref:{V8_COMMIT}", base=Path.cwd())
    assert arm.release is not None
    module = arm.release.module
    species, profile, references = _fixture_species(), _fixture_profile(), _fixture_references()
    context = TaxonContext(
        taxon_id=1,
        common_name="Test Bird",
        scientific_name="Avis test",
        family="Testidae",
        summary="<p>A test bird.</p>",
        source_url="https://birds.example/taxon/1",
    )
    # The fixture prompts as v8's own test rendered them for its recorded digest.
    rendered = [
        module.profile_prompt(species, context, references, _FIXTURE_DOMAINS),
        module.profile_prompt(
            species,
            context,
            references,
            _FIXTURE_DOMAINS,
            prior_profile=profile,
            profile_conflicts=(_fixture_conflict(),),
        ),
        module.plate_prompt(species, profile, references, Path("candidate.png")),
        module.plate_prompt(
            species,
            profile,
            references,
            Path("candidate.png"),
            ("Shorten the tail",),
            invariant_findings=("Keep the wing pattern",),
            has_correction_source=True,
        ),
        module.review_prompt(species, profile, references, _FIXTURE_DOMAINS),
        module.review_prompt(
            species,
            profile,
            references,
            _FIXTURE_DOMAINS,
            prior_corrections=("Repair the visible leg",),
            prior_profile_conflicts=(_fixture_conflict(),),
        ),
    ]

    digest = hashlib.sha256("\n\0\n".join(rendered).encode("utf-8")).hexdigest()

    assert module.PROMPT_VERSION == "field-journal-v8"
    assert digest == RENDERED_PROMPT_SHA256["field-journal-v8"]
    assert arm.label == f"field-journal-v8@{V8_COMMIT[:12]}"
    assert not arm.is_default
    # A first attempt, called as v8's runner called it: the plate named by absolute path.
    output = Path("/srv/plate-eval/run/v8/1/1/generated.png")
    prompt = arm.render(species, profile, references, output, output.parent)
    assert prompt == module.plate_prompt(species, profile, references, output)
    assert prompt.endswith(
        f"{output}\n\nVerify that the file exists before finishing. Do not"
        " merely describe the image.\n"
    )


@needs_v8
def test_a_release_arm_is_recorded_with_its_module_and_sent_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, panel, _ = make_run(tmp_path)
    arm = parse_arm(f"v8=ref:{V8_COMMIT}", base=tmp_path)
    assert arm.release is not None
    register_arm(run, arm)

    record = json.loads(run.path("arms", "v8.json").read_text())
    assert record["kind"] == "release" and record["commit"] == V8_COMMIT
    assert record["prompt_version"] == "field-journal-v8"
    source = subprocess.run(
        ["git", "-C", str(REPOSITORY), "show", f"{V8_COMMIT}:src/inky_bird_frame/prompts.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert run.path("arms", "v8.prompts.py").read_text() == source
    assert record["sha256"] == hashlib.sha256(source.encode("utf-8")).hexdigest()

    captured = tmp_path / "prompt"
    stub = tmp_path / "codex"
    stub.write_text(
        "#!/bin/sh\n"
        f"cat > {shlex.quote(str(captured))}\n"
        f"cp {shlex.quote(str(panel[0].portrait_path))} generated.png\n"
    )
    stub.chmod(0o755)
    references = frozen_references(run, panel[0])
    done, report = generate_samples(
        run,
        CodexGenerator(stub, timeout_seconds=30, model=None),
        [arm],
        [(panel[0], references)],
        1,
        budget=Budget(1),
    )
    assert report.generated == 1 and ("v8", panel[0].taxon_id, 1) in done
    output = (sample_dir(run, arm, panel[0], 1) / "generated.png").resolve()
    expected = arm.release.module.plate_prompt(
        panel[0].bird(), panel[0].profile, list(references.photos), output
    )
    assert captured.read_text() == expected

    # The absolute path that release sends must not carry a home folder.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with pytest.raises(HarnessError, match="outside your home folder"):
        register_arm(run, parse_arm(f"v8b=ref:{V8_COMMIT}", base=tmp_path))
    assert not run.path("arms", "v8b.json").exists()


@needs_v8
def test_a_release_arm_keeps_its_release_for_the_run(tmp_path: Path) -> None:
    run, panel, _ = make_run(tmp_path)
    register_arm(run, parse_arm(f"v8=ref:{V8_COMMIT}", base=tmp_path))

    with pytest.raises(HarnessError, match="other wording"):
        register_arm(run, parse_arm("v8=ref:HEAD", base=tmp_path))
    generator = ColourGenerator()
    generate_samples(
        run,
        generator,
        [parse_arm(f"v8=ref:{V8_COMMIT}", base=tmp_path)],
        [(panel[0], frozen_references(run, panel[0]))],
        1,
        budget=Budget(1),
    )
    assert generator.calls == [("v8", panel[0].taxon_id)]


def test_unusable_release_references_are_refused(tmp_path: Path) -> None:
    for ref in ("--upload-pack=x", "no-such-ref-anywhere", "a b"):
        with pytest.raises(HarnessError):
            parse_arm(f"x=ref:{ref}", base=tmp_path)
