# Plate prompt evaluation and tuning

A development tool that decides, without the owner in the loop, whether a
change to the plate prompt (or the review prompt) makes better plates. It is
not part of the `inky-bird` package, the app never imports it, and CI only runs
its offline tests.

It has five parts:

- **A fixed species panel** (`panel.json`): 20 approved catalog species across
  ten body plans, 12 for tuning (`dev`) and 8 held out (`holdout`). A prompt is
  only ever accepted on the held-out species.
- **Frozen inputs.** Each species uses its approved catalog profile and the
  catalog's own reference photos, downloaded once into the run directory and
  checksummed. The prompt wording is the only thing that changes.
- **Three blind judges** (two Claude, one Codex by default). Each sees two
  anonymised plates, `plate-A.png` and `plate-B.png`, in a random order drawn
  per pair, plus the reference photos, the rubric (`rubric.md`) and the exact
  text the plate should carry. It returns a defect list per plate, which plate
  is anatomically more correct, which has more correct lettering, and whether
  each is publishable.
- **Calibration** before the judges are trusted: anchors, planted defects and
  side-swap consistency.
- **Statistics and a fixed decision rule.** Pairs are decided by majority of
  judges; win rates carry a species-clustered bootstrap interval.

## Running it

From the repository root. The harness has its own small project
(`evals/pyproject.toml`) so its optimiser dependency never reaches the app:

```bash
uv run --with-editable ./evals inky-plate-eval --help
```

Every command takes `--run-dir`, prints one JSON object, and can be rerun after
any interruption: finished plates, verdicts and reference photos are never
paid for twice. Run `cache-references` once in each new run directory; `var/`
is ignored by Git.

```bash
RUN=var/plate-eval/2026-11-v8-v9

# 1. Download and freeze the reference photos (network, no cost).
uv run --with-editable ./evals inky-plate-eval cache-references --run-dir "$RUN"

# 2. Calibrate the judges (judge calls only, no image generation).
uv run --with-editable ./evals inky-plate-eval calibrate --run-dir "$RUN" \
  --anchors path/to/anchors --dry-run
uv run --with-editable ./evals inky-plate-eval calibrate --run-dir "$RUN" \
  --anchors path/to/anchors --parallel 3

# 3. Compare two prompts: the first arm is the candidate, the second the baseline.
uv run --with-editable ./evals inky-plate-eval compare --run-dir "$RUN" \
  --max-generations 120 --dry-run v9=default v8=evals/plate_tuning/arms/v8.txt
uv run --with-editable ./evals inky-plate-eval compare --run-dir "$RUN" \
  --max-generations 120 --parallel 2 v9=default v8=evals/plate_tuning/arms/v8.txt

# 4. Tune the review prompt on plates the panel has already judged.
uv run --with-editable ./evals inky-plate-eval tune-review --run-dir "$RUN" \
  --labels-from "$RUN" --budget 60 --max-reviews 160 --dry-run

# 5. Tune the plate prompt, then test the best candidate on held-out species.
TUNE=var/plate-eval/2026-11-tune
uv run --with-editable ./evals inky-plate-eval cache-references --run-dir "$TUNE"
uv run --with-editable ./evals inky-plate-eval tune --run-dir "$TUNE" \
  --budget 150 --max-generations 220 --dry-run

# 6. Tables and contact sheets.
uv run --with-editable ./evals inky-plate-eval report --run-dir "$RUN"
```

An arm is `NAME=default` (the shipped `PLATE_INSTRUCTIONS`) or `NAME=PATH` to a
text file holding replacement instruction wording. `arms/v8.txt` is the
field-journal-v8 wording placed in the current safe template: the data block,
attribution removal and output path cannot change, so v8's inline profile
values are read from the data block instead. v8 also gave every species the
skimmer bill and pupil notes; the template still adds them for skimmers, so a
Black Skimmer gets those notes twice under this arm.

Options common to every command: `--config FILE` (TOML, see below),
`--catalog` (default `catalog`), `--panel`, `--parallel N` (concurrent paid
calls, default 1) and `--seed` (only when creating a run directory).

## What costs money

| Command | Image generations | Judge calls | Other |
| --- | --- | --- | --- |
| `cache-references` | none | none | downloads reference photos |
| `calibrate` | none | (3 per species + anchors) x 2 orders x judges | |
| `compare` | missing plates, at most `--max-generations` | pairs x judges | |
| `tune` | at most `--max-generations` | per new pair x judges | one reflection call per proposal |
| `tune-review` | none | none | Codex reviews, at most `--max-reviews` |

`--max-generations` (and `--max-reviews`) has no default: a paid command does
not start without it, and stops starting new calls when it is reached. Use
`--dry-run` first; it prints the plan and spends nothing. `tune` also refuses a
cap below what its held-out test could need, so it never stops halfway with an
untestable winner. Every paid call is logged in `generations.jsonl`,
`judgments.jsonl`, `reviews.jsonl` or `reflections.jsonl`.

Generations and reviews use the local `codex` login; judges and reflection use
the local `claude` and `codex` logins. No API keys are read or passed: child
processes get the application's environment allowlist.

## The decision rule

Fixed in advance (`stats.decide`). The candidate replaces the baseline only if,
on the held-out species:

1. every held-out species has at least one judged pair;
2. the 95% species-clustered bootstrap interval of the anatomy win rate lies
   wholly above 0.5;
3. the lettering win rate is at least 0.5;
4. its fatal-defect rate is no higher than the baseline's.

A pair's outcome is the strict majority of its judges (otherwise a tie, scored
0.5); a plate is fatal or acceptable when a strict majority of judges says so.
Resampling whole species keeps the samples of one species, which share inputs,
from looking more independent than they are. Every summary also reports
pairwise judge agreement and Fleiss' kappa.

For the review prompt the score per labelled plate is half for agreeing with
the panel's pass/fail and half for the overlap of defect categories; the tuned
review is accepted only when the interval of its held-out score gain lies above
zero.

The owner remains the final authority: a passing result is the case for a
prompt change, not the change itself. Adopting a winner means a normal pull
request that updates `PLATE_INSTRUCTIONS`, bumps `PROMPT_VERSION` and records
the new digest.

## Calibration

`calibrate` judges pairs whose better plate is known, each twice with the sides
swapped:

- **Anchors** (`--anchors DIR`): `DIR/<taxon_id>/<name>.png` files, each a
  plate known to be worse than that species' approved catalog plate (for
  example a first attempt the pipeline review rejected). A judge is right when
  it prefers the approved plate on at least one axis and on neither prefers the
  worse one.
- **Planted defects**, made from each approved plate: a stretched lower third,
  a duplicated region where the legs usually are, and a replaced Length line
  with wrong digits. The judge must report a defect of a matching category on
  the planted plate.
- **Swap consistency**: both orders must get the same answers.

Thresholds are in the configuration (0.8 each by default). Without anchors a
judge cannot pass. A judge that fails is dropped from the configuration, or the
rubric is fixed and calibration rerun, before any comparison is trusted.

## Tuning

`tune` uses GEPA (reflective prompt evolution) through its adapter interface:
the candidate is `{"plate_instructions": text}`; each evaluation generates a
plate for a dev species and judges it blind against the cached baseline plate
for that species. The score is weighted from anatomy, lettering and
acceptability (`anatomy_weight`, `lettering_weight`, `acceptable_weight`), and
the judges' defect lists are the feedback GEPA reflects on with a Claude model
(`reflection_model`).

Every candidate passes `safety.validate_candidate` before anything is
generated: each template placeholder exactly once, a length cap, no URLs, and
the prompt-safety invariants (one data block no value escapes, no photo
attribution, intact reference numbering, unchanged tool request and output
path). A rejected candidate scores zero and costs nothing. GEPA keeps its own
state in `gepa/` and resumes from it.

## Configuration

Optional TOML; every key has a default (`config.py`). For example:

```toml
samples_per_species = 3
reflection_model = "claude-fable-5-2"

[[judges]]
name = "fable-1"
kind = "claude"
model = "claude-fable-5-2"

[[judges]]
name = "fable-2"
kind = "claude"
model = "claude-fable-5-2"

[[judges]]
name = "codex-1"
kind = "codex"
```

### Adding a judge

Add a `[[judges]]` entry with a unique name, a `kind` of `claude` or `codex`
and optionally a model, then run `calibrate` again. A new kind of judge is a
class with a `name` and `judge(request) -> BlindVerdict` (see `judges.py`) that
reads only `request.blind_dir`, plus a branch in `judges.build_judges`.

The command lines are built in `judges.claude_command`, `judges.codex_command`
and `tune.ClaudeReflector.command`. They were written without running the real
tools; check them against `claude --help` and `codex exec --help` before the
first paid run.

## The run directory

```
run.json                  seed (blind sides and pair ids derive from it)
references/<taxon>/       frozen photos and their manifest
arms/<arm>.json           each arm's exact wording and digest
<arm>/<taxon>/<k>/        prompt.txt, codex.log, generated.png, portrait.png, sample.json
blind/<pair>/             what a judge sees: plate-A.png, plate-B.png, references/, RUBRIC.md
keys/<pair>.json          which plate is A; never inside a judged folder
verdicts/<pair>/<judge>.json
compare/<first>-vs-<second>/summary.json and pairs.json (unblinded)
calibration/, tune/, tune-review/, gepa/, reviews/, report/
```

A sample is finished only when `sample.json` is written, after the portrait.
A crash before that leaves either nothing (generated again) or a
`generated.png` (prepared again without paying). Blind folders and frozen
references are built in a hidden staging folder and published by one rename.
Judges run in the blind folder; Codex's read-only sandbox can still read other
files on disk, so the key files are separated, not secret.
