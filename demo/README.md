# Demo film

Builds the README film from the real app running on loopback with sample data.
Scenes, captions and narration live in `storyboard.json`.

## Requirements

- Node.js 20+, the project `.venv`, and `ffmpeg`/`ffprobe` on `PATH`.
- Playwright with Chromium, installed as in CONTRIBUTING.md, with
  `INKY_QA_PLAYWRIGHT` pointing at it.

The first voiced run downloads the Kokoro voice model (about 340 MB).

## Build

```bash
demo/make-demo.sh               # voiced film into demo/out/
demo/make-demo.sh --no-voice    # silent film
```

The script fails if a scene leaves loopback, the film falls outside 45–75 s,
or a size budget is exceeded. Review `demo/out/contact-sheet.png` and the film
before publishing.

To narrate with ElevenLabs instead of Kokoro, set `ELEVENLABS_API_KEY` and
`INKY_DEMO_ELEVENLABS=1`. Word-level pronunciation fixes go in
`voice/pronunciations.json`.

Remotion, used for composition, is free for individuals and companies of up to
three people.
