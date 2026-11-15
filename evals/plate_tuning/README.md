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
  each is publishable. The two Claude judges share one model and prompt, so
  their votes are strongly correlated: the majority leans on that model, and
  agreement and kappa overstate how independent the panel is. The report says
  so whenever two judges share a model.
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
paid for twice. Only one command at a time can use a run directory; a second
is refused. Run `cache-references` once in each new run directory.

Keep run directories outside every repository. Plates are generated inside the
run directory, and a Codex agent working inside a repository reads that
repository's guidance about these very prompts.

```bash
RUN="$HOME/plate-eval/2026-11-v8-v9"

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
TUNE="$HOME/plate-eval/2026-11-tune"
uv run --with-editable ./evals inky-plate-eval cache-references --run-dir "$TUNE"
uv run --with-editable ./evals inky-plate-eval tune --run-dir "$TUNE" \
  --budget 150 --max-generations 220 --dry-run

# 6. Tables and contact sheets.
uv run --with-editable ./evals inky-plate-eval report --run-dir "$RUN"
```

An arm is `NAME=default` (the shipped `PLATE_INSTRUCTIONS`) or `NAME=PATH` to a
text file holding replacement instruction wording. Every arm passes the same
safety gate as a tuning candidate (see Tuning) before anything is generated.
`arms/v8.txt` is a reconstruction of the field-journal-v8 wording placed in
the current safe template, not the shipped v8 prompt: the data block,
attribution removal and output path cannot change, so v8's inline profile
values are read from the data block instead. "v8 against v9" therefore
measures the instruction wording only, and the report says so for every arm
that is not the shipped wording. v8 also gave every species the skimmer bill
and pupil notes; the template still adds them for skimmers, so a Black Skimmer
gets those notes twice under this arm.

The comparison in step 3 decides on the 8 held-out species only (24 pairs at
3 samples), so it has little power to separate close prompts. Looking at
held-out results before tuning also weakens their standing as an untouched
test for a later `tune`; use a fresh run directory for tuning, as step 5 does.

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
not start without it. The cap is for the whole run directory: every generation
the ledger shows started, by this command or an earlier one, failed attempts
included, counts against it, and no new call starts once it is reached. Giving
a larger cap on a later command allows only the difference. Use `--dry-run`
first; it prints the plan and the cap's `used` and `remaining` and spends
nothing. `tune` also refuses a remaining cap below what its held-out test could
need, so it never stops halfway with an untestable winner. Every paid call is
logged in `generations.jsonl`, `judgments.jsonl`, `reviews.jsonl` or
`reflections.jsonl`. Judge calls have no cap; the dry runs count them.

For the orchestrated first run: `calibrate` with 6 planted species and A anchor
plates makes (18 + A) x 2 pairs and at most 3 judge calls per pair, and no
generations. `compare` of v9 against v8 on all 20 species at 3 samples needs
120 generations (`--max-generations 120`) and at most 180 judge calls.

Generations and reviews use the local `codex` login; judges and reflection use
the local `claude` and `codex` logins. No API keys are read or passed: child
processes get the application's environment allowlist.

## The decision rule

Fixed in advance (`stats.decide`). The candidate replaces the baseline only if,
on the held-out species:

1. no judge is missing on more than 10% of the pairs (`max_missing_judge_share`),
   so the decision rests on the whole panel; `summary.json` lists each judge's
   verdicts and gaps;
2. every held-out species has at least one judged pair;
3. the 95% species-clustered bootstrap interval of the anatomy win rate lies
   wholly above 0.5;
4. the lettering win rate is at least 0.5;
5. its fatal-defect rate is no higher than the baseline's.

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
- **Planted defects**, made from each approved plate: the lower body and legs
  stretched within the bird's column (the margin lettering and the ruler are
  untouched), a duplicated region where the legs usually are, and a replaced
  Length line with wrong digits. A region that holds too little drawing is
  refused rather than planted, so a defect is never pasted paper on paper.
  The judge finds the defect only when it reports a matching category more
  often on the planted plate than on the untouched original in the same
  verdict; a complaint it makes about every plate does not count. Where on the
  plate it says the defect is, is not checked.
- **Planted preference**: the judge must also prefer the untouched plate.
- **Swap consistency**: both orders must get the same answers.

Thresholds are in the configuration (0.8 each by default). Without anchors a
judge cannot pass. A judge that fails is dropped from the configuration, or the
rubric is fixed and calibration rerun, before any comparison is trusted. Each
verdict records the rubric's digest and the judge's kind and model; after a
change to any of them, the next command judges those pairs again (and rebuilds
the blind folders with the new rubric) instead of reusing old verdicts.

## Tuning

`tune` uses GEPA (reflective prompt evolution) through its adapter interface:
the candidate is `{"plate_instructions": text}`; each evaluation generates a
plate for a dev species and judges it blind against the cached baseline plate
for that species. The score is weighted from anatomy, lettering and
acceptability (`anatomy_weight`, `lettering_weight`, `acceptable_weight`), and
the judges' defect lists are the feedback GEPA reflects on with a Claude model
(`reflection_model`). The reflection call has no tools at all and runs in an
empty temporary folder with none of the user's settings, so held-out results
in the run directory cannot shape a proposal. A failed generation is retried
(`generation_attempts`, default 2, within the cap) before a candidate scores
zero for it, so flaky generation is not read as a worse prompt; a baseline
plate that cannot be made stops tuning.

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
first paid run, in particular `--setting-sources`, the `Read(//...)` path rule,
`--tools ""` and the Codex `-c` overrides.

### What a judge can see

Each judge call gets a fresh copy of one pair (the two plates, the references
and the rubric, all with one fixed file time) in a new folder under the system
temporary folder, which is removed after the verdict. The folder is outside the
run directory and outside any repository (a temporary folder inside a project
is refused), so no key, other judge's verdict or project guidance is reachable
by a relative path. Codex's schema and answer files sit in a second temporary
folder.

- Claude judges may use only `Read`, scoped to that folder, with no MCP
  servers and no user settings, hooks, plugins or guidance
  (`--setting-sources project`, and the project is the empty folder).
- Codex judges run in the `read-only` sandbox with web search, MCP servers and
  project guidance turned off. That sandbox still lets the agent read any file
  your account can read by its absolute path, and Codex still loads your global
  `AGENTS.md` under its home folder if you have one. A judge is never told
  where the run directory is, but nothing stops a judge that searched the disk
  for it; keep run directories out of the temporary folder.

## The run directory

```
run.json                  seed (blind sides and pair ids derive from it)
references/<taxon>/       frozen photos and their manifest
arms/<arm>.json           each arm's exact wording and digest
<arm>/<taxon>/<k>/        prompt.txt, codex.log, generated.png, portrait.png, sample.json
blind/<pair>/             what a judge sees: plate-A.png, plate-B.png, references/, RUBRIC.md
unblinding/<key>.json     which plate is A; its name is unrelated to the pair's
verdicts/<pair>/<judge>.json   stamped with the rubric digest and judge model
compare/<first>-vs-<second>/summary.json and pairs.json (unblinded, run-relative paths)
.lock                     held by the command using the run directory
calibration/, tune/, tune-review/, gepa/, reviews/, report/
```

A sample is finished only when `sample.json` is written, after the portrait.
A plate is prepared again without paying only when the ledger shows its
generation finished; a plate from a failed, timed-out or possibly unfinished
generation is deleted and generated again, so it is never judged. If the
harness itself is killed outright (not stopped with Ctrl-C or a normal
terminate), an agent it started can keep running: stop it before resuming.
Blind folders and frozen references are built in a hidden staging folder and
published by one rename.
