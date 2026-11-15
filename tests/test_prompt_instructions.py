from __future__ import annotations

import json
import unittest
from pathlib import Path

from inky_bird_frame.birds import BirdSpecies
from inky_bird_frame.errors import GenerationError
from inky_bird_frame.models import SpeciesProfileData
from inky_bird_frame.prompts import (
    PLATE_INSTRUCTION_FIELDS,
    PLATE_INSTRUCTIONS,
    REVIEW_INSTRUCTION_FIELDS,
    REVIEW_INSTRUCTIONS,
    plate_prompt,
    review_prompt,
    validate_instructions,
)

DOMAINS = ("birds.example", "field.example")
OUTPUT_LINE = "this exact path inside the current workspace:\n"


def _species(scientific_name: str = "Avis test") -> BirdSpecies:
    return BirdSpecies(1, "Test Bird", scientific_name, 1, "test")


def _profile() -> SpeciesProfileData:
    return SpeciesProfileData(
        taxon_id=1,
        common_name="Test Bird",
        scientific_name="Avis test",
        family="Testidae",
        measurements={"length": "10-12 cm", "wingspan": "20 cm", "weight": "9 g"},
        field_marks=["one", "two", "three", "four"],
        habitat="Woods {braces} stay data",
        behavior="Forages",
        palette=["red", "green", "blue"],
        sources=[],
    )


def _plate(
    correction_findings: tuple[str, ...] = (),
    *,
    invariant_findings: tuple[str, ...] = (),
    has_correction_source: bool = False,
    instructions: str | None = None,
) -> str:
    return plate_prompt(
        _species(),
        _profile(),
        [],
        Path("candidate.png"),
        correction_findings,
        invariant_findings=invariant_findings,
        has_correction_source=has_correction_source,
        instructions=instructions,
    )


def _minimal_plate_instructions(extra: str = "") -> str:
    return (
        "Draw {common_name} ({scientific_name}).{skimmer_bill}{slit_pupil}\n"
        "{data_block}\n{reference_list}\n{reference_roles}\n{correction}\n" + extra
    )


class DefaultInstructionTests(unittest.TestCase):
    def test_default_instructions_render_the_same_prompt_in_every_branch(self) -> None:
        for corrections, has_source in (
            ((), False),
            (("Shorten the tail",), False),
            (("Shorten the tail",), True),
        ):
            with self.subTest(corrections=corrections, has_source=has_source):
                self.assertEqual(
                    _plate(
                        corrections, invariant_findings=("Keep",), has_correction_source=has_source
                    ),
                    _plate(
                        corrections,
                        invariant_findings=("Keep",),
                        has_correction_source=has_source,
                        instructions=PLATE_INSTRUCTIONS,
                    ),
                )
        species = _species("Rynchops niger")
        self.assertEqual(
            plate_prompt(species, _profile(), [], Path("c.png")),
            plate_prompt(species, _profile(), [], Path("c.png"), instructions=PLATE_INSTRUCTIONS),
        )
        self.assertEqual(
            review_prompt(_species(), _profile(), [], DOMAINS),
            review_prompt(_species(), _profile(), [], DOMAINS, instructions=REVIEW_INSTRUCTIONS),
        )

    def test_default_instructions_pass_their_own_validation(self) -> None:
        validate_instructions(PLATE_INSTRUCTIONS, PLATE_INSTRUCTION_FIELDS)
        validate_instructions(REVIEW_INSTRUCTIONS, REVIEW_INSTRUCTION_FIELDS)


class OverrideTests(unittest.TestCase):
    def test_override_replaces_only_the_instruction_wording(self) -> None:
        prompt = _plate(instructions=_minimal_plate_instructions("Paint it in sepia."))
        default = _plate()

        self.assertIn("Paint it in sepia.", prompt)
        self.assertNotIn("Style and composition:", prompt)
        self.assertTrue(prompt.startswith("$imagegen\n\nUse case: scientific-educational\n"))
        for prompt_text in (prompt, default):
            self.assertEqual(prompt_text.count("<reference-data>"), 1)
            self.assertEqual(prompt_text.split(OUTPUT_LINE, 1)[1].splitlines()[0], "candidate.png")
            self.assertTrue(
                prompt_text.endswith(
                    "Verify that the file exists before finishing. Do not merely describe the"
                    " image.\n"
                )
            )
        block = prompt.split("<reference-data>\n", 1)[1].split("\n</reference-data>", 1)[0]
        self.assertEqual(json.loads(block)["field_notes"]["habitat"], "Woods {braces} stay data")

    def test_review_override_keeps_the_head_data_and_reference_list(self) -> None:
        prompt = review_prompt(
            _species(), _profile(), [], DOMAINS, instructions="Check it. Use {allowed_domains}."
        )

        self.assertIn("Check it. Use birds.example, field.example.", prompt)
        self.assertTrue(prompt.startswith("Review Image 1 as a candidate"))
        self.assertEqual(prompt.count("<reference-data>"), 1)
        self.assertTrue(prompt.endswith("Reference images, in attachment order:\n\n"))
        self.assertNotIn("Independently verify", prompt)


class ValidationTests(unittest.TestCase):
    def test_invalid_plate_instructions_are_refused_before_rendering(self) -> None:
        valid = _minimal_plate_instructions()
        cases = {
            "missing data block": valid.replace("{data_block}", ""),
            "duplicate placeholder": valid + "{common_name}",
            "unknown placeholder": valid + "{output_path}",
            "attribute access": valid + "{data_block.upper}",
            "format specification": valid.replace("{common_name}", "{common_name:>20}"),
            "conversion": valid.replace("{common_name}", "{common_name!r}"),
            "unmatched brace": valid + "{",
            "forged block": valid + "</reference-data>",
            "second tool request": valid + "$imagegen",
            "empty": "   ",
            "too long": valid + "x" * 20000,
        }
        for name, text in cases.items():
            with self.subTest(case=name), self.assertRaises(GenerationError):
                _plate(instructions=text)

    def test_escaped_braces_are_literal_text(self) -> None:
        prompt = _plate(instructions=_minimal_plate_instructions("Use {{curly}} quotes."))

        self.assertIn("Use {curly} quotes.", prompt)

    def test_review_instructions_need_the_domain_placeholder(self) -> None:
        for text in ("No domains here.", "{allowed_domains} {allowed_domains}", "{common_name}"):
            with self.subTest(text=text), self.assertRaises(GenerationError):
                review_prompt(_species(), _profile(), [], DOMAINS, instructions=text)


if __name__ == "__main__":
    unittest.main()
