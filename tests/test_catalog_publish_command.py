from __future__ import annotations

import io
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from inky_bird_frame.cli import catalog_publish_command
from inky_bird_frame.config import NotificationEvent
from inky_bird_frame.errors import CatalogPublishError


def _result(skipped: list[dict[str, str]]) -> dict[str, object]:
    return {
        "changed": True,
        "published": [{"taxon_id": 1, "common_name": "Example Bird"}],
        "skipped": skipped,
        "merged": True,
    }


class CatalogPublishCommandTests(unittest.TestCase):
    def _run(
        self,
        *,
        dry_run: bool,
        result: dict[str, object] | None = None,
        error: Exception | None = None,
    ) -> tuple[MagicMock, MagicMock, str]:
        output = io.StringIO()
        with (
            patch("inky_bird_frame.cli._config"),
            patch(
                "inky_bird_frame.cli.run_catalog_publish",
                return_value=result,
                side_effect=error,
            ) as publish,
            patch("inky_bird_frame.cli.safe_record_degradation") as degrade,
            patch("inky_bird_frame.cli.safe_record_recovery") as recover,
            redirect_stdout(output),
        ):
            if error is None:
                self.assertEqual(catalog_publish_command(Namespace(dry_run=dry_run)), 0)
            else:
                with self.assertRaises(type(error)):
                    catalog_publish_command(Namespace(dry_run=dry_run))
            self.assertEqual(publish.call_args.kwargs, {"dry_run": dry_run})
        return degrade, recover, output.getvalue()

    def test_records_recovery_after_a_clean_publication(self) -> None:
        degrade, recover, output = self._run(dry_run=False, result=_result([]))

        degrade.assert_not_called()
        self.assertEqual(recover.call_count, 1)
        self.assertEqual(recover.call_args.kwargs["key"], "catalog-publication")
        self.assertEqual(recover.call_args.kwargs["event"], NotificationEvent.PUBLICATION_RECOVERED)
        self.assertIn('"merged": true', output)

    def test_skipped_species_are_a_degradation_that_names_them(self) -> None:
        skipped = [
            {"species": "2-rüppell-warbler", "reason": "manifest.json mentions Exampleville, ES"},
            {"species": "3-second-bird", "reason": "species/3-second-bird/display.png checksum"},
        ]

        degrade, recover, _ = self._run(dry_run=False, result=_result(skipped))

        recover.assert_not_called()
        self.assertEqual(degrade.call_count, 1)
        kwargs = degrade.call_args.kwargs
        self.assertEqual(kwargs["key"], "catalog-publication")
        self.assertEqual(kwargs["event"], NotificationEvent.PUBLICATION_ERROR)
        body = kwargs["body"]
        self.assertIn("2-rüppell-warbler", body)
        self.assertIn("3-second-bird", body)
        self.assertIn("did not pass the publication checks", body)
        for private in ("Exampleville", "manifest.json", "display.png", "species/"):
            self.assertNotIn(private, body)
            self.assertNotIn(private, kwargs["title"])

    def test_skipped_names_that_are_not_species_directories_are_not_shown(self) -> None:
        skipped = [
            {"species": "notes from Exampleville", "reason": "unexpected"},
            {"species": "../private", "reason": "unexpected"},
            *({"species": f"{taxon}-bird", "reason": "checksum"} for taxon in range(10, 22)),
        ]

        degrade, recover, _ = self._run(dry_run=False, result=_result(skipped))

        recover.assert_not_called()
        body = degrade.call_args.kwargs["body"]
        self.assertIn("14 species", body)
        self.assertNotIn("Exampleville", body)
        self.assertNotIn("private", body)
        self.assertIn("10-bird", body)
        self.assertNotIn("21-bird", body)
        self.assertIn("and 4 more", body)

    def test_failure_records_a_degradation_without_details(self) -> None:
        degrade, recover, _ = self._run(
            dry_run=False, error=CatalogPublishError("git push failed: Exampleville")
        )

        recover.assert_not_called()
        self.assertEqual(degrade.call_count, 1)
        self.assertEqual(degrade.call_args.kwargs["key"], "catalog-publication")
        self.assertNotIn("Exampleville", degrade.call_args.kwargs["body"])

    def test_dry_run_records_neither_degradation_nor_recovery(self) -> None:
        for name, result, error in (
            ("clean", _result([]), None),
            ("skipped", _result([{"species": "2-second-bird", "reason": "checksum"}]), None),
            ("failure", None, CatalogPublishError("git fetch failed")),
        ):
            with self.subTest(name):
                degrade, recover, _ = self._run(dry_run=True, result=result, error=error)
                degrade.assert_not_called()
                recover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
