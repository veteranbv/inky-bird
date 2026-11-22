"""The narration map changes only what an engine is told to say."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("synthesize", ROOT / "demo/voice/synthesize.py")
assert SPEC is not None and SPEC.loader is not None
SYNTHESIZE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SYNTHESIZE)


def test_spoken_rewrites_whole_words_per_engine() -> None:
    line = "Inky Bird is MIT licensed. Browse the gallery at henrysowell.com/birds."
    kokoro = SYNTHESIZE.spoken(line, "kokoro")
    assert kokoro == (
        "ink-ee Bird is em eye tee licensed. Browse the gallery at henry sowel dot com slash birds."
    )
    eleven = SYNTHESIZE.spoken(line, "elevenlabs")
    assert eleven == (
        "Inky Bird is M.I.T. licensed. Browse the gallery at henry sowel dot com slash birds."
    )
    # Whole words only: nothing inside other words is rewritten.
    assert SYNTHESIZE.spoken("inked plates", "kokoro") == "inked plates"


def test_captions_keep_the_true_spellings() -> None:
    text = json.dumps(json.loads((ROOT / "demo/storyboard.json").read_text()))
    assert "MIT licensed" in text and "henrysowell.com/birds" in text
    assert "Sowel " not in text and "em eye tee" not in text and "M I T" not in text
