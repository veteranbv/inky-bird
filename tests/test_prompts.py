from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from inky_bird_frame.birds import BirdSpecies, TaxonContext
from inky_bird_frame.errors import GenerationError
from inky_bird_frame.models import ProfileConflict, ReferencePhoto, SpeciesProfileData
from inky_bird_frame.prompts import PROMPT_VERSION, plate_prompt, profile_prompt, review_prompt


class PromptTests(unittest.TestCase):
    def test_prompt_contract_version(self) -> None:
        self.assertEqual(PROMPT_VERSION, "field-journal-v9")

    def test_edit_target_does_not_freeze_anatomy_flagged_for_redraw(self) -> None:
        species = BirdSpecies(1, "Test Bird", "Avis test", 1, "test")
        profile = SpeciesProfileData(
            taxon_id=1,
            common_name="Test Bird",
            scientific_name="Avis test",
            family="Testidae",
            measurements={"length": "10 cm", "wingspan": "20 cm", "weight": "30 g"},
            field_marks=["one", "two", "three", "four"],
            habitat="Woods",
            behavior="Perches",
            palette=["red", "green", "blue"],
            sources=[],
        )
        prompt = plate_prompt(
            species,
            profile,
            [],
            Path("candidate.png"),
            ("Redraw both feet and recompose the primary body",),
            invariant_findings=("Keep the accepted wing markings",),
            has_correction_source=True,
        )
        normalized = " ".join(prompt.split())
        self.assertIn("Current corrections take precedence", normalized)
        self.assertIn("redraw that whole affected structure", normalized)
        self.assertIn("not a frozen outline or pose", normalized)
        self.assertIn("never retain defective geometry", normalized)
        self.assertIn("structural change is visibly present", normalized)
        self.assertIn("Keep the accepted wing markings", normalized)
        self.assertIn("Keep every unflagged factual statement", normalized)
        self.assertNotIn("smallest complete edit", normalized)

    def test_plate_prompt_contains_species_facts_and_excludes_location(self) -> None:
        species = BirdSpecies(12942, "Eastern Bluebird", "Sialia sialis", 26, "iNaturalist")
        profile = SpeciesProfileData(
            taxon_id=12942,
            common_name="Eastern Bluebird",
            scientific_name="Sialia sialis",
            family="Turdidae",
            measurements={"length": "7 in", "wingspan": "12 in", "weight": "1 oz"},
            field_marks=["blue head", "blue back", "rufous breast", "white belly"],
            habitat="Open woodland",
            behavior="Drops from perches to forage",
            palette=["blue", "rufous", "white"],
            sources=[
                {"title": "A", "url": "https://example.test/a"},
                {"title": "B", "url": "https://example.test/b"},
            ],
        )
        reference = ReferencePhoto(
            1,
            2,
            "observer",
            "Photo by observer",
            "cc-by",
            "https://example.test/observation",
            "https://example.test/image.jpg",
            1600,
            1200,
            "image.jpg",
            "a" * 64,
        )

        prompt = plate_prompt(species, profile, [reference], Path("candidate.png"))

        self.assertIn("$imagegen", prompt)
        self.assertIn("blue head", prompt)
        self.assertNotIn("Photo by observer", prompt)
        self.assertNotIn("12345", prompt)
        self.assertNotIn("Exampleville", prompt)

    def test_plate_prompt_includes_review_corrections(self) -> None:
        species = BirdSpecies(12942, "Eastern Bluebird", "Sialia sialis", 26, "iNaturalist")
        profile = SpeciesProfileData(
            taxon_id=12942,
            common_name="Eastern Bluebird",
            scientific_name="Sialia sialis",
            family="Turdidae",
            measurements={"length": "7 in", "wingspan": "12 in", "weight": "1 oz"},
            field_marks=["blue head", "blue back", "rufous breast", "white belly"],
            habitat="Open woodland",
            behavior="Drops from perches to forage",
            palette=["blue", "rufous", "white"],
            sources=[
                {"title": "A", "url": "https://example.test/a"},
                {"title": "B", "url": "https://example.test/b"},
            ],
        )

        prompt = plate_prompt(
            species,
            profile,
            [],
            Path("candidate.png"),
            ("Correct the wing bars",),
        )

        self.assertIn("Correct the wing bars", prompt)
        self.assertIn("Create a new image", prompt)

    def test_profile_and_review_prompts_include_bounded_conflict_history(self) -> None:
        species = BirdSpecies(1, "Test Bird", "Avis test", 1, "test")
        profile = SpeciesProfileData(
            taxon_id=1,
            common_name="Test Bird",
            scientific_name="Avis test",
            family="Testidae",
            measurements={"length": "10 cm", "wingspan": "20 cm", "weight": "30 g"},
            field_marks=["one", "two", "three", "four"],
            habitat="Woods",
            behavior="Perches",
            palette=["red", "green", "blue"],
            sources=[
                {"title": "One", "url": "https://birds.example/one"},
                {"title": "Two", "url": "https://field.example/two"},
            ],
        )
        conflict = ProfileConflict(
            **{
                "field": "measurements.length",
                "profile_value": "10 cm",
                "observed_value": "12 cm",
                "sources": [
                    {"title": "One", "url": "https://birds.example/length"},
                    {"title": "Two", "url": "https://field.example/length"},
                ],
            }
        )
        context = TaxonContext(
            taxon_id=1,
            common_name="Test Bird",
            scientific_name="Avis test",
            family="Testidae",
            summary="A test bird.",
            source_url="https://birds.example/taxon/1",
        )

        research = profile_prompt(
            species,
            context,
            [],
            ("birds.example", "field.example"),
            prior_profile=profile,
            profile_conflicts=(conflict,),
        )
        review = review_prompt(
            species,
            profile,
            [],
            ("birds.example", "field.example"),
            prior_corrections=("Repair the visible leg",),
            prior_profile_conflicts=(conflict,),
        )
        normalized_research = " ".join(research.split())
        normalized_review = " ".join(review.split())

        self.assertIn("one bounded re-adjudication", normalized_research)
        self.assertIn(
            "Do not assume either the prior profile or the review claim", normalized_research
        )
        self.assertIn('"field": "measurements.length"', research)
        self.assertIn("Earlier review history for convergence checking", normalized_review)
        self.assertIn("Repair the visible leg", normalized_review)
        self.assertIn("resolved_corrections only when", normalized_review)
        self.assertIn("must exactly match one earlier correction", normalized_review)
        self.assertIn("never repeat a stale profile_value", normalized_review)
        self.assertIn(
            "drop a conflict that the current direct sources no longer support", normalized_review
        )
        self.assertIn(
            "Do not report a profile conflict merely because another allowed source publishes a "
            "different supported set",
            normalized_review,
        )
        self.assertIn("required to use one compatible published measurement set", normalized_review)

    def test_plate_and_review_prompts_enforce_schematic_ruler(self) -> None:
        species = BirdSpecies(4496, "Black Skimmer", "Rynchops niger", 26, "iNaturalist")
        profile = SpeciesProfileData(
            taxon_id=12942,
            common_name="Eastern Bluebird",
            scientific_name="Sialia sialis",
            family="Turdidae",
            measurements={"length": "7 in", "wingspan": "12 in", "weight": "1 oz"},
            field_marks=["blue head", "blue back", "rufous breast", "white belly"],
            habitat="Open woodland",
            behavior="Drops from perches to forage",
            palette=["blue", "rufous", "white"],
            sources=[
                {"title": "A", "url": "https://example.test/a"},
                {"title": "B", "url": "https://example.test/b"},
            ],
        )

        prompt = plate_prompt(
            species,
            profile,
            [],
            Path("candidate.png"),
            ("Shorten the tail",),
            invariant_findings=("Keep the accepted body proportions",),
            has_correction_source=True,
        )
        normalized_prompt = " ".join(prompt.split())

        self.assertIn("Use case: precise-object-edit", prompt)
        self.assertIn("Image 1 is the previous plate and the edit target", prompt)
        self.assertIn("Current actionable corrections", prompt)
        self.assertIn("Non-regression constraints", prompt)
        self.assertIn("Keep the accepted body proportions", prompt)
        self.assertIn("not a request to redraw a feature", normalized_prompt)
        self.assertIn("Do not shrink the bird", prompt)
        self.assertIn('Label it "BODY LENGTH: " followed by that exact length', normalized_prompt)
        self.assertIn('"length": "7 in"', prompt)
        self.assertIn("SCHEMATIC — NOT TO SCALE", prompt)
        self.assertIn("minimum at the bottom and maximum at the top", prompt)
        self.assertIn("roughly even, unlabeled interior ticks", normalized_prompt)
        self.assertIn("count and spacing do not encode measurement units", normalized_prompt)
        self.assertIn("Do not draw a zero-based or wider full-range axis", prompt)
        self.assertIn("do not add a separate range bracket", normalized_prompt)
        self.assertIn("Match both the direction and degree", normalized_prompt)
        self.assertIn("the lower mandible is the jaw below the gape line", normalized_prompt)
        self.assertIn("its tip must be farther from the shared bill base", normalized_prompt)
        self.assertIn("Preserve the near-full length of the shorter structure", prompt)
        self.assertIn("do not substitute a wider gape, lengthen the wrong jaw", normalized_prompt)
        self.assertIn("the lower tip must also reach farther forward", normalized_prompt)
        self.assertIn("do not rely on a steeper gape angle", normalized_prompt)
        self.assertIn("same plausible proportional relationship", normalized_prompt)
        self.assertIn("keep the iris dark enough to blend", normalized_prompt)
        self.assertIn("render the supplied pupil shape precisely", normalized_prompt)
        self.assertIn("When the supplied shape is vertical", normalized_prompt)
        self.assertIn(
            "thin black vertical slit rather than a round or oval pupil", normalized_prompt
        )
        self.assertIn("Never invent a vertical slit", normalized_prompt)
        self.assertIn("compare every visible text line character-for-character", normalized_prompt)
        self.assertNotIn("Undo any incidental", normalized_prompt)
        self.assertNotIn("Do not copy or lightly edit", prompt)

        review = review_prompt(species, profile, [], ("example.test", "example.org"))
        normalized_review = " ".join(review.split())

        self.assertIn("values must increase from bottom to top", review)
        self.assertIn("no separate marker may disagree with it", normalized_review)
        self.assertIn(
            "exact count or minor spacing variation is not a factual error", normalized_review
        )
        self.assertIn("wrong values or units", normalized_review)
        self.assertIn("remains a material text error", normalized_review)
        self.assertIn("Read every visible text line in full", normalized_review)
        self.assertIn("duplicated, omitted, substituted, or nonsensical words", normalized_review)
        self.assertIn(
            "trace each one independently from its shared bill base to its tip", normalized_review
        )
        self.assertIn("ambiguous, co-terminal, or reversed rendering", normalized_review)
        self.assertIn("do not confuse the open-gape angle", normalized_review)
        self.assertIn("Compare both the direction and degree", normalized_review)
        self.assertIn("truncated-looking upper mandible", normalized_review)
        self.assertIn("inconsistent proportions between heads", normalized_review)
        self.assertIn("Do not infer a seasonal-plumage correction", normalized_review)
        self.assertIn(
            "explicit agreement from at least two direct allowed source pages", normalized_review
        )
        self.assertIn("exactly one complete primary specimen", normalized_review)
        self.assertIn(
            "detached wing and bill/head studies are intentional supplementary anatomy",
            normalized_review,
        )
        self.assertIn("validate each study independently", normalized_review)
        self.assertIn("naturally occluded far-side wing or leg is acceptable", normalized_review)
        self.assertIn("never excuse a malformed", normalized_review)
        self.assertIn("at least two direct HTTPS sources", normalized_review)
        self.assertIn("Do not instruct the image generator", normalized_review)
        self.assertIn("Identity fields are not adjudicable", normalized_review)
        self.assertNotIn("exactly four evenly spaced", normalized_prompt)
        self.assertNotIn("exactly four evenly spaced", normalized_review)


_FIXTURE_DOMAINS = ("birds.example", "field.example")


_FIXTURE_WORKSPACE = Path("/srv/example-user/workspace")


def _fixture_species() -> BirdSpecies:
    return BirdSpecies(1, "Test Bird", "Avis test", 1, "test")


def _fixture_profile() -> SpeciesProfileData:
    return SpeciesProfileData(
        taxon_id=1,
        common_name="Test Bird",
        scientific_name="Avis test",
        family="Testidae",
        measurements={"length": "10 cm", "wingspan": "20 cm", "weight": "30 g"},
        field_marks=["one", "two", "three", "four"],
        habitat="Woods",
        behavior="Perches",
        palette=["red", "green", "blue"],
        sources=[
            {"title": "One", "url": "https://birds.example/one"},
            {"title": "Two", "url": "https://field.example/two"},
        ],
    )


def _fixture_references() -> list[ReferencePhoto]:
    return [
        ReferencePhoto(
            index,
            100 + index,
            f"observer-{index}",
            f"Photo by observer-{index}",
            "cc-by",
            f"https://example.test/observations/{100 + index}",
            f"https://example.test/photos/{index}/large.jpg",
            1600,
            1200,
            f"0{index}-{index}.jpg",
            str(index) * 64,
        )
        for index in (1, 2, 3)
    ]


def _fixture_conflict() -> ProfileConflict:
    return ProfileConflict(
        field="measurements.length",
        profile_value="10 cm",
        observed_value="12 cm",
        sources=[
            {"title": "One", "url": "https://birds.example/length"},
            {"title": "Two", "url": "https://field.example/length"},
        ],
    )


def _rendered_prompts() -> list[str]:
    species = _fixture_species()
    profile = _fixture_profile()
    references = _fixture_references()
    context = TaxonContext(
        taxon_id=1,
        common_name="Test Bird",
        scientific_name="Avis test",
        family="Testidae",
        summary="<p>A test bird.</p>",
        source_url="https://birds.example/taxon/1",
    )
    return [
        profile_prompt(species, context, references, _FIXTURE_DOMAINS),
        profile_prompt(
            species,
            context,
            references,
            _FIXTURE_DOMAINS,
            prior_profile=profile,
            profile_conflicts=(_fixture_conflict(),),
        ),
        plate_prompt(species, profile, references, Path("candidate.png")),
        plate_prompt(
            species,
            profile,
            references,
            Path("candidate.png"),
            ("Shorten the tail",),
            invariant_findings=("Keep the wing pattern",),
            has_correction_source=True,
        ),
        review_prompt(species, profile, references, _FIXTURE_DOMAINS),
        review_prompt(
            species,
            profile,
            references,
            _FIXTURE_DOMAINS,
            prior_corrections=("Repair the visible leg",),
            prior_profile_conflicts=(_fixture_conflict(),),
        ),
        plate_prompt(_skimmer(), profile, references, Path("candidate.png")),
        plate_prompt(
            _skimmer(),
            profile,
            references,
            Path("candidate.png"),
            ("Lengthen the lower mandible",),
            invariant_findings=("Keep the slit pupil",),
            has_correction_source=True,
        ),
        *(
            plate_prompt(
                species,
                profile,
                references,
                _FIXTURE_WORKSPACE / ".inky-bird-frame-generation/1-attempt-01-x/generated.png",
                ("Shorten the tail",),
                invariant_findings=("Keep the wing pattern", "Keep the pale eye"),
                has_correction_source=has_source,
                owner_findings=("Keep the pale eye",),
                workspace=_FIXTURE_WORKSPACE,
            )
            for has_source in (True, False)
        ),
    ]


# One entry per prompt version: the SHA-256 of the fixture prompts above.
# Changing any prompt text needs a new PROMPT_VERSION and a new entry here.
RENDERED_PROMPT_SHA256 = {
    "field-journal-v7": "84079b7217e69381494122bdee62257b08f9b216b2c9094e566e90239cf90816",
    "field-journal-v8": "9164b21301e4386d75ef442cb6a1db00f93443fbfbdd7f0bae9a45551975e877",
    "field-journal-v9": "a968aa16fad7980af65de822ac3ba09660a29dd393fedda7283eeeac0297d1a5",
}


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _skimmer() -> BirdSpecies:
    return BirdSpecies(4496, "Black Skimmer", "Rynchops niger", 1, "test")


class PlateWordingTests(unittest.TestCase):
    def test_posture_fits_the_species_and_perch_is_plain(self) -> None:
        prompt = _normalized(
            plate_prompt(_fixture_species(), _fixture_profile(), [], Path("candidate.png"))
        )

        self.assertNotIn("natural perched posture", prompt)
        self.assertIn(
            "a natural resting posture for the species (perched, standing, swimming or in flight,"
            " as fits it)",
            prompt,
        )
        self.assertIn(
            "If perched, show a short plain twig or bare ground and no other scenery.", prompt
        )
        self.assertIn("Quiet margins. No scenery,", prompt)

    def test_skimmer_and_slit_pupil_notes_only_for_skimmers(self) -> None:
        skimmer_notes = (
            "For an open skimmer bill",
            "In a side-profile skimmer with an open bill",
            "thin black vertical slit",
            "Never invent a vertical slit",
        )
        ordinary = _normalized(
            plate_prompt(_fixture_species(), _fixture_profile(), [], Path("candidate.png"))
        )
        skimmer = _normalized(
            plate_prompt(_skimmer(), _fixture_profile(), [], Path("candidate.png"))
        )
        for note in skimmer_notes:
            with self.subTest(note=note):
                self.assertNotIn(note, ordinary)
                self.assertIn(note, skimmer)
        general = "When a field mark depends on unequal paired structures such as mandibles"
        self.assertIn(general, ordinary)
        self.assertIn(general, skimmer)

    def test_final_check_does_not_ask_to_undo_drift(self) -> None:
        prompt = _normalized(
            plate_prompt(_fixture_species(), _fixture_profile(), [], Path("candidate.png"))
        )

        self.assertNotIn("drift", prompt)
        self.assertIn("Generate exactly one image", prompt)
        self.assertIn("compare every visible text line character-for-character", prompt)

    def test_constraints_are_labelled_by_origin(self) -> None:
        for has_source in (True, False):
            with self.subTest(has_correction_source=has_source):
                prompt = plate_prompt(
                    _fixture_species(),
                    _fixture_profile(),
                    [],
                    Path("candidate.png"),
                    ("Shorten the tail",),
                    invariant_findings=("Owner note", "Review note"),
                    has_correction_source=has_source,
                    owner_findings=("Owner note",),
                )
                normalized = _normalized(prompt)
                self.assertNotIn("human review", normalized)
                self.assertIn("From the owner:", normalized)
                self.assertIn("From earlier automated review:", normalized)
                data = json.loads(
                    prompt.split("<reference-data>\n", 1)[1].split("\n</reference-data>", 1)[0]
                )
                self.assertEqual(data["constraints_from_the_owner"], ["Owner note"])
                self.assertEqual(data["constraints_from_automated_review"], ["Review note"])
                self.assertEqual(data["corrections"], ["Shorten the tail"])

    def test_constraints_of_unknown_origin_are_not_called_human(self) -> None:
        prompt = plate_prompt(
            _fixture_species(),
            _fixture_profile(),
            [],
            Path("candidate.png"),
            (),
            invariant_findings=("Earlier note",),
        )
        normalized = _normalized(prompt)

        self.assertNotIn("human review", normalized)
        self.assertNotIn("From the owner", normalized)
        self.assertIn('"constraints_from_earlier_review": [', prompt)

    def test_output_is_named_relative_to_the_workspace(self) -> None:
        workspace = Path("/srv/example-user/workspace")
        output = workspace / ".inky-bird-frame-generation" / "1-attempt-01-x" / "generated.png"
        prompt = plate_prompt(
            _fixture_species(),
            _fixture_profile(),
            [],
            output,
            workspace=workspace,
        )

        self.assertNotIn("/srv", prompt)
        self.assertNotIn("example-user", prompt)
        tail = prompt.split("this exact path inside the current workspace:\n", 1)[1]
        self.assertEqual(
            tail.splitlines()[0], ".inky-bird-frame-generation/1-attempt-01-x/generated.png"
        )

    def test_edit_constraints_keep_their_accepted_review_header(self) -> None:
        prompt = _normalized(
            plate_prompt(
                _fixture_species(),
                _fixture_profile(),
                [],
                Path("candidate.png"),
                ("Shorten the tail",),
                invariant_findings=("Owner note",),
                has_correction_source=True,
                owner_findings=("Owner note",),
            )
        )

        self.assertIn(
            "Non-regression constraints from earlier accepted reviews: - From the owner:", prompt
        )

    def test_output_outside_the_workspace_is_refused(self) -> None:
        with self.assertRaises(GenerationError):
            plate_prompt(
                _fixture_species(),
                _fixture_profile(),
                [],
                Path("/elsewhere/generated.png"),
                workspace=Path("/srv/example/workspace"),
            )


class PromptReferenceNumberingTests(unittest.TestCase):
    def test_review_provenance_numbers_references_after_the_candidate(self) -> None:
        references = _fixture_references()
        prompt = review_prompt(_fixture_species(), _fixture_profile(), references, _FIXTURE_DOMAINS)

        provenance = (
            prompt.split("Reference images, in attachment order:\n", 1)[1].strip().splitlines()
        )

        self.assertEqual(provenance[0], "Image 2: reference photo")
        self.assertEqual(len(provenance), len(references))
        self.assertEqual(provenance[-1], f"Image {len(references) + 1}: reference photo")
        self.assertNotIn("Image 1: reference photo", prompt)


class PromptVersionTests(unittest.TestCase):
    def test_version_fixtures_cover_every_gated_branch(self) -> None:
        rendered = _normalized("\n".join(_rendered_prompts()))

        for text in (
            "For an open skimmer bill",
            "In a side-profile skimmer with an open bill",
            "thin black vertical slit",
            "From the owner:",
            "From earlier automated review:",
            "From earlier review:",
            ".inky-bird-frame-generation/1-attempt-01-x/generated.png",
        ):
            with self.subTest(text=text):
                self.assertIn(text, rendered)
        self.assertNotIn(_FIXTURE_WORKSPACE.as_posix(), rendered)

    def test_rendered_prompt_text_matches_its_version(self) -> None:
        digest = hashlib.sha256("\n\0\n".join(_rendered_prompts()).encode("utf-8")).hexdigest()

        self.assertIn(
            PROMPT_VERSION,
            RENDERED_PROMPT_SHA256,
            "Add a RENDERED_PROMPT_SHA256 entry for the new PROMPT_VERSION",
        )
        self.assertEqual(
            RENDERED_PROMPT_SHA256[PROMPT_VERSION],
            digest,
            "Prompt text changed: bump PROMPT_VERSION and record the new digest",
        )

    def test_each_prompt_version_has_distinct_text(self) -> None:
        digests = list(RENDERED_PROMPT_SHA256.values())
        self.assertEqual(len(digests), len(set(digests)))


if __name__ == "__main__":
    unittest.main()
