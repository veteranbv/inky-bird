# Inky Bird demo film

A reproducible, code-generated demo of the real app: a narrated ~65 second
1080p film, a silent captioned variant, a looping GIF, a poster, a
contact sheet and a review manifest. One command regenerates everything from
a clean checkout with free, local tools.

## What the film shows, and what is synthetic

Every UI scene is the checked-out app running on loopback with a synthetic
fixture: a disposable owner created before recording starts, five public
approved plates from `catalog/` (Northern Cardinal, American Robin, Eastern
Bluebird, Blue Jay, American Goldfinch), and a "Garden birds" collection made
through the real browser UI. Generation is off, no sources are configured, and every non-loopback request is aborted and logged. UI
scenes carry a visible **Sample data** pill.

Two scenes are not screen recordings and say so by design: the sources scene
uses plain text chips (no third-party logos), and the wall-frame scene is a
photograph (`docs/images/framed-installation.jpg`) entered on a deliberate
hard cut — browser Play never drives the physical wall frame.

## Prerequisites

- Node.js 20+ and the project `.venv` (`uv sync --extra dev --locked`, see
  CONTRIBUTING.md).
- Playwright pinned like CI, installed outside the checkout, with Chromium:

  ```bash
  qa_tools=$(mktemp -d)
  npm install --prefix "$qa_tools" --ignore-scripts --no-audit --no-fund --save-exact \
    playwright@1.62.1 playwright-core@1.62.1
  "$qa_tools/node_modules/.bin/playwright" install chromium
  export INKY_QA_PLAYWRIGHT="$qa_tools/node_modules/playwright"
  ```

- `ffmpeg` and `ffprobe` on PATH.
- Narration: Kokoro (Apache-2.0, offline CPU) through `kokoro-onnx`. The first
  voiced run creates `demo/voice/.venv` and downloads the model files into
  `demo/voice/models/` (~340 MB, one time). No account, no key.
- Composition: Remotion, isolated in `demo/video/` with its own pinned
  `package.json` and lockfile (`npm ci` runs automatically on first use).
  Remotion's license is free for individuals and companies of up to three
  people; the capture and audio stages do not depend on it.

## Regenerate

```bash
demo/make-demo.sh                 # everything, voiced, into demo/out/
demo/make-demo.sh --no-voice      # silent film, no voice tooling needed
demo/make-demo.sh --stills-only   # capture + per-scene stills, no full render
demo/make-demo.sh --scene frame   # re-capture one scene and its still
```

The script exits non-zero when any check fails: a scene that left loopback or
raised a page error, a duration outside 45-75 s, a first decoded frame that
does not match the poster, loudness off -16 LUFS by more than 1 LU when
voiced, or any size budget (MP4 < 10 MB, GIF < 5 MB, poster < 1 MB).

### Optional ElevenLabs narration

Kokoro stays the default so the rebuild needs no key. If `ELEVENLABS_API_KEY`
is set in the environment **and** `INKY_DEMO_ELEVENLABS=1` is set, narration
uses ElevenLabs instead. The key is read only from the environment and is
never printed, logged or written anywhere. The published film uses the stock
ElevenLabs voice George (`voice.elevenlabsVoice` in `storyboard.json`);
`ELEVENLABS_VOICE_ID` overrides it for a one-off render.

## Outputs (`demo/out/`, never committed)

| File | What it is |
|------|------------|
| `inky-demo.mp4` | Narrated film: 1920x1080, 30 fps, H.264 High yuv420p, AAC 48 kHz, -16 LUFS, faststart, metadata stripped |
| `inky-demo-silent.mp4` | The same film without the narration track (captions are burned in) |
| `inky-demo.gif` | Looping GIF (journal → Play → wall plate), 960 px wide |
| `inky-demo-poster.jpg` | Poster whose pixels match the film's first decoded frame |
| `contact-sheet.png` | 3x3 sheet, one frame per scene |
| `stills/<scene>.png` | Full-resolution still of every scene, rendered before the film |
| `inky-demo-transcript.md`, `inky-demo.vtt` | Transcript and caption track |
| `media-review.json` | Durations, dimensions, loudness, sizes, sha256 of every output, per-scene events, blocked requests, page errors |
| `capture/` | Raw per-scene clips, `*.events.json`, end-of-scene screenshots |

Review by opening the contact sheet and every still, then the film. The
per-scene `events.json` lists each action with its timestamp, so every caption
claim can be matched to something actually shown.

## How it is built

1. **Capture** — `tests/web/capture-demo.cjs` records each scene as its own
   1920x1080 clip over the shared fixture server in
   `tests/web/demo-fixture.cjs` (also used by `tests/web/capture-readme.cjs`).
   A small fake cursor glides to each click target because headless Chromium
   draws none.
2. **Voice** — `demo/voice/synthesize.py` reads `storyboard.json` and writes
   one WAV per scene plus measured durations.
3. **Timeline** — `demo/compose-timeline.cjs` merges the storyboard minimums
   with the narration lengths (a sentence is never cut), caps scene holds by
   usable clip length, lays out crossfades and the hard cut, and pulls the
   app's own `:root` CSS tokens so cards and captions use the app's type and
   color.
4. **Compose** — the Remotion project in `demo/video/` renders a still per
   scene, then the film.
5. **Export** — `demo/export.cjs` normalizes loudness, enforces every budget,
   builds the GIF/poster/contact sheet/captions, and writes
   `media-review.json`.

The storyboard (`demo/storyboard.json`) is the single source of truth for
scenes, captions and narration; `demo/narration.md` is its human-readable
mirror. `tests/web/demo-storyboard.test.mjs` keeps both honest (length window,
captions present, no forbidden claims).
