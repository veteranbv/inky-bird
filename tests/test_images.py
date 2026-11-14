from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image, ImageCms, PngImagePlugin
from png_fixtures import write_truncated_png

from inky_bird_frame.errors import GenerationError
from inky_bird_frame.images import (
    COMPACT_HARDWARE_SIZE,
    HARDWARE_SIZE,
    PAPER_COLOR,
    PORTRAIT_SIZE,
    ROTATION_DEGREES,
    SUPPORTED_HARDWARE_SIZES,
    prepare_generated_plate,
    slugify,
)


class ImageHelperTests(unittest.TestCase):
    def test_slugify(self) -> None:
        self.assertEqual(slugify("Eastern Bluebird"), "eastern-bluebird")
        self.assertEqual(slugify("Sialia sialis!"), "sialia-sialis")
        self.assertEqual(slugify("Anna's Hummingbird"), "anna-s-hummingbird")
        self.assertEqual(slugify("Piopío"), "piopío")

    def test_canonical_assets_match_inky_panel(self) -> None:
        self.assertEqual(PORTRAIT_SIZE, (1200, 1600))
        self.assertEqual(HARDWARE_SIZE, (1600, 1200))
        self.assertEqual(COMPACT_HARDWARE_SIZE, (800, 480))
        self.assertEqual(SUPPORTED_HARDWARE_SIZES, {(1600, 1200), (800, 480)})
        self.assertEqual(ROTATION_DEGREES, 90)


class GeneratedPlateTests(unittest.TestCase):
    def prepare(self, source: Path) -> tuple[Path, Path]:
        portrait = source.parent / "out/portrait.png"
        display = source.parent / "out/display.png"
        prepare_generated_plate(source, portrait, display)
        return portrait, display

    def test_plate_is_written_at_exact_panel_sizes_in_rgb(self) -> None:
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "generated.png"
            Image.new("RGB", (900, 1200), (10, 120, 30)).save(source)

            portrait, display = self.prepare(source)

            with Image.open(portrait) as image:
                portrait_facts = (image.format, image.mode, image.size, image.getpixel((600, 800)))
            with Image.open(display) as image:
                display_facts = (image.format, image.mode, image.size)

        self.assertEqual(portrait_facts, ("PNG", "RGB", PORTRAIT_SIZE, (10, 120, 30)))
        self.assertEqual(display_facts, ("PNG", "RGB", HARDWARE_SIZE))

    def test_transparency_is_composited_onto_paper(self) -> None:
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "generated.png"
            Image.new("RGBA", (300, 400), (0, 0, 0, 0)).save(source)

            portrait, _ = self.prepare(source)

            with Image.open(portrait) as image:
                pixel = image.getpixel((10, 10))

        self.assertEqual(pixel, PAPER_COLOR)

    def test_colour_profile_and_text_metadata_are_not_carried_into_the_plate(self) -> None:
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "generated.png"
            profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
            text = PngImagePlugin.PngInfo()
            text.add_text("Comment", "Saved at Exampleville")
            Image.new("RGB", (300, 400), "white").save(source, icc_profile=profile, pnginfo=text)

            portrait, display = self.prepare(source)

            with Image.open(portrait) as image:
                portrait_info = dict(image.info)
            with Image.open(display) as image:
                display_info = dict(image.info)

        self.assertEqual(portrait_info, {})
        self.assertEqual(display_info, {})

    def test_truncated_image_raises_generation_error(self) -> None:
        with TemporaryDirectory() as temporary:
            source = write_truncated_png(Path(temporary) / "generated.png", size=(300, 400))

            with self.assertRaisesRegex(GenerationError, "could not be prepared"):
                self.prepare(source)

    def test_non_image_raises_generation_error(self) -> None:
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "generated.png"
            source.write_text("not an image")

            with self.assertRaisesRegex(GenerationError, "could not be prepared"):
                self.prepare(source)

    def test_decompression_bomb_raises_generation_error(self) -> None:
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "generated.png"
            Image.new("RGB", (300, 400), "white").save(source)

            with (
                patch.object(Image, "MAX_IMAGE_PIXELS", 1000),
                self.assertRaisesRegex(GenerationError, "could not be prepared"),
            ):
                self.prepare(source)

    def test_missing_output_raises_generation_error(self) -> None:
        with TemporaryDirectory() as temporary, self.assertRaises(GenerationError):
            self.prepare(Path(temporary) / "generated.png")


if __name__ == "__main__":
    unittest.main()
