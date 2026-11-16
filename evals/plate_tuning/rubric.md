# Blind paired plate assessment

You are an expert ornithological illustrator and bird anatomist. This folder
holds two candidate field-journal plates of one bird species, `plate-A.png` and
`plate-B.png`, and real reference photographs of the species in `references/`.
You do not know how either plate was made or which is newer; the letters are
assigned at random. Judge only what you see. Neither plate is assumed to be
good: both may be flawed, or both may be fine.

Look at every reference photo first, then each plate at full size. For each
plate, check the primary bird and each detail study against the references and
your knowledge of the species.

## Categories

Report every concrete defect under exactly one of these categories:

1. `legs` — where each leg joins the body (both legs arise close together under
   the belly), leg length, toe count and arrangement, grip on the perch or
   surface, nothing floating, no extra or missing visible leg.
2. `wings` — direction and attachment, fold, primary projection relative to the
   tail, shape and proportion of the wing study.
3. `head_bill` — neck length and thickness, head size, bill length, shape and
   colour, eye position, colour and pupil.
4. `tail` — length relative to body and wingtips, shape of the tip, a tail that
   wingtips hide or impersonate.
5. `proportions` — silhouette, body shape, stretched or squashed parts, posture
   the species never takes, duplicated or pasted-looking regions.
6. `plumage` — pattern and colour against the references.
7. `lettering` — every printed label and value. Compare each one with the
   expected text supplied in the task, character for character: names, family,
   Length, Wingspan, Weight, Habitat, Behavior, each field mark and each swatch
   name. A wrong digit, a missing or duplicated label, a misspelling or a raw
   data key name printed on the page is a lettering defect.
8. `ruler` — the body-length ruler: endpoints and units must match the expected
   length, minimum at the bottom, labelled as schematic, separate from the bird.

## Severity

- `fatal` — the plate could not be published: wrong species, a missing,
  extra or detached limb, head or tail, an impossible posture, or a wrong
  measurement value.
- `major` — clearly wrong and visible at normal viewing size.
- `minor` — a small inaccuracy an expert would notice.

For each defect give where it is on the plate (for example "primary bird,
left leg" or "left margin, Weight line") and which reference photo contradicts
it (its file name), or `expected text` for lettering and ruler defects.

## Verdict

- `more_correct_anatomy`: which plate is anatomically more correct overall
  (legs, wings, head and bill, tail, proportions, plumage) — `A`, `B` or `tie`.
  Use `tie` only when you genuinely cannot separate them.
- `more_correct_lettering`: which plate's lettering and ruler are more correct
  against the expected text — `A`, `B` or `tie`.
- `acceptable` for each plate: would you accept it into a published field
  journal as it is? A plate with any fatal defect is never acceptable.

Do not let the position of a plate (left or first) influence you.
