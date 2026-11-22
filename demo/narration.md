# Demo film narration

This is the human-readable script. The machine-read copy lives in
`storyboard.json` (`scenes[].narration`); edit both together. Scene holds
stretch to fit each narration clip, so a longer line makes a longer scene.

| # | Scene | Line |
|---|-------|------|
| 1 | Title card | Inky Bird. A field journal for the birds in your life. |
| 2 | Northern Cardinal plate | The cardinal at your feeder deserves a page. |
| 3 | Sources | Bring in nearby sightings, station detections, and your own birding history. Use one source, several, or none. |
| 4 | Journal and bird page | Every plate is AI illustrated, checked against references, and reviewed before approval. Find a bird, open its page, and add it to your favorites. |
| 5 | Collections | Keep collections for your garden, or for a trip. |
| 6 | Play | Press Play to fill any browser screen. Plates change once a minute, and Next skips ahead. |
| 7 | Manage | Pair a read-only screen, and keep everything on your own network. |
| 8 | E-paper wall frame photo | An optional color e-paper frame can show the same approved plates. |
| 9 | End card | Inky Bird is MIT licensed and self hosted. Browse the gallery at henrysowell.com/birds. |

Pronunciation fixes live in `demo/voice/pronunciations.json`. They change only
what the narrator is sent; captions and the transcript keep the real spelling.

Nothing in the script may claim that browser Play updates the wall frame, that
plates are hand-painted, or that public nearby sightings are the owner's own
sightings. `tests/web/demo-storyboard.test.mjs` enforces that.
