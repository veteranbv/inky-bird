# Plate prompt evaluation and tuning

A development tool that decides, without the owner in the loop, whether a
change to the plate prompt makes better plates. It is
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
  separately for every pair and judge, plus the reference photos, the rubric
  (`rubric.md`) and the exact text the plate should carry. It returns a defect list per plate, which plate
  is anatomically more correct, which has more correct lettering, and whether
  each is publishable. The two Claude judges share one model and prompt, so
  their votes are strongly correlated: the majority leans on that model, and
  agreement and kappa overstate how independent the panel is. The report says
  so whenever two judges share a model.
- **Calibration** before the judges are trusted: anchors, planted defects and
  side-swap consistency. No decision is made without a passing calibration
  for the same rubric, judging prompt, thresholds and models.
- **Statistics and a fixed decision rule.** Pairs are decided by majority of
  the calibrated judges; win rates carry a species-clustered bootstrap
  interval.

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

Keep run directories outside every repository and outside your home folder,
for example under `/Users/Shared/plate-eval/` on a Mac. Plates are generated
inside the run directory, and a Codex agent working inside a repository reads
that repository's guidance about these very prompts. A release arm (below)
sends the plate's absolute path in the prompt, as that release did, so it is
refused in a run directory under the home folder; keep run directories out of
the system temporary folder too (see "What a judge can see").

```bash
RUN=/Users/Shared/plate-eval/2026-11-v8-v9

# 1. Download and freeze the reference photos (network, no cost). Add
#    --species with the taxon ids of any off-panel anchor species.
uv run --with-editable ./evals inky-plate-eval cache-references --run-dir "$RUN"

# 2. Check each judge's command line with one real call each (3 judge calls).
uv run --with-editable ./evals inky-plate-eval judge-smoke --run-dir "$RUN"

# 3. Calibrate the judges (judge calls only, no image generation).
uv run --with-editable ./evals inky-plate-eval calibrate \
  --run-dir "$RUN" --anchors path/to/anchors --dry-run
uv run --with-editable ./evals inky-plate-eval calibrate \
  --run-dir "$RUN" --anchors path/to/anchors --parallel 3

# 4. Compare two prompts: the first arm is the candidate, the second the baseline.
#    v8 is the whole plate prompt exactly as that release rendered it.
V8=bd2ee8caf12b530e1aaa14a102274ebdedd9fc83
uv run --with-editable ./evals inky-plate-eval compare \
  --run-dir "$RUN" --max-generations 120 --dry-run v9=default "v8=ref:$V8"
uv run --with-editable ./evals inky-plate-eval compare \
  --run-dir "$RUN" --max-generations 120 --parallel 2 v9=default "v8=ref:$V8"

# 5. Tune the plate prompt in a fresh run directory, then test the best
#    candidate on held-out species. It reuses the first run's calibration.
TUNE=/Users/Shared/plate-eval/2026-11-tune
uv run --with-editable ./evals inky-plate-eval cache-references --run-dir "$TUNE"
uv run --with-editable ./evals inky-plate-eval tune \
  --run-dir "$TUNE" --calibration "$RUN/calibration/summary.json" \
  --budget 150 --max-generations 220 --dry-run

# 6. Tables, contact sheets and token totals.
uv run --with-editable ./evals inky-plate-eval report --run-dir "$RUN"
```

An arm is one of:

- `NAME=default`: the shipped `PLATE_INSTRUCTIONS` in the current template.
- `NAME=PATH`: a text file of replacement instruction wording in the current
  template. It passes the same safety gate as a tuning candidate (see Tuning).
- `NAME=ref:GITREF`: the plate prompt exactly as that commit rendered it. The
  harness reads that commit's `src/inky_bird_frame/prompts.py` with
  `git show` (read only), saves it as `arms/NAME.prompts.py` in the run
  directory, loads it as a separate module and renders each first attempt with
  that module's own `plate_prompt`, called as that release's runner called it.
  The text is byte for byte what the release would send (the v8 commit above is
  checked against v8's recorded prompt digest in the tests), including the
  photo attribution and the plate's absolute path that v8 printed. It is not
  gated, because it is a record of what shipped, not a candidate. Pin a commit
  rather than a moving branch: an arm name keeps its prompt for the whole run.

Plates are generated a (species, sample) pair at a time, every arm's plate for
that pair next to each other, the pairs in an order drawn from the run's seed,
so drift over a long run, or the point where the cap stops it, does not line
up with an arm. The cap is taken for a whole pair at once. A pair that still
lacks a plate (a failed generation) is not judged and is listed under
`pairs_dropped`; running the same command again retries it.

The comparison in step 4 decides on the 8 held-out species only (24 pairs at
3 samples), so it has little power to separate close prompts. Looking at
held-out results before tuning also weakens their standing as an untouched
test for a later `tune`; use a fresh run directory for tuning, as step 5 does.

Options common to every command: `--config FILE` (TOML, see below),
`--catalog` (default `catalog`), `--panel`, `--parallel N` (concurrent paid
calls, default 1) and `--seed` (only when creating a run directory). `compare`
and `tune` also take `--calibration FILE` (another run's
`calibration/summary.json`) and `--allow-uncalibrated` (see Calibration).

## What costs money

| Command | Image generations | Judge calls | Other |
| --- | --- | --- | --- |
| `cache-references` | none | none | downloads reference photos |
| `judge-smoke` | none | one per judge | |
| `calibrate` | none | (3 per species + anchors) x 2 orders x judges judgments | |
| `compare` | missing plates, at most `--max-generations` | pairs x judges judgments | |
| `tune` | at most `--max-generations` | per new pair x judges judgments | one reflection call per proposal |

A judgment is one judge's verdict on one pair; a failed call is retried, so
each judgment makes at most `judge_attempts` (default 2) paid calls. The dry
runs print both: `judgments_needed_at_most` (`judgments_at_most` for
`calibrate`) counts judgments, `judge_calls_at_most` the paid calls including
retries.

`--max-generations` has no default: a paid command does
not start without it. The cap is for the whole run directory: every generation
the ledger shows started, by this command or an earlier one, failed attempts
included, counts against it, and no new call starts once it is reached. Giving
a larger cap on a later command allows only the difference. Use `--dry-run`
first; it prints the plan and the cap's `used` and `remaining` and spends
nothing. `tune` sets the held-out test's generations aside before the
optimiser starts (every candidate plate plus any missing baseline plate) and
gives the optimiser only the rest, and it refuses to start when the remaining
cap is below the optimiser's planned share plus that reserve. The optimiser
cannot spend the reserve, even when GEPA finishes an iteration past
`max_metric_calls`: it stops before a batch it cannot pay for in full, and
the best candidate scored on every dev species goes to the held-out test. Every paid call is
logged in `generations.jsonl`, `judgments.jsonl` or `reflections.jsonl`. Judge calls have no cap; the dry runs count them.

For the orchestrated first run (3 judges): `judge-smoke` makes 3 judge calls.
`calibrate` with 6 planted species and A anchor plates makes (18 + A) x 2
pairs, so (36 + 2A) x 3 judgments (up to twice that in paid calls with
retries), and no generations. `compare` of v9
against v8 on all 20 species at 3 samples needs 120 generations
(`--max-generations 120`) and 60 pairs, 180 judgments (at most 360 paid judge
calls). `tune --budget 150` needs at most 150
+ 12 dev baselines + 48 reserved (24 candidate and 24 baseline held-out
plates) = 210 generations, at most one judged pair (3 judgments) per
candidate plate, 72 judgments for the held-out test (each judgment up to two
paid calls), and one reflection per proposal.
A resumed `tune` subtracts the candidate plates earlier attempts already made
on the dev species, which count as used, so resuming never needs a higher cap.
The candidate chosen for the held-out test is recorded (`tune/selection.json`)
before the test starts; a tune resumed after that does not run the optimiser
again and needs only the held-out plates still missing.

Every finished call records the tokens its tool reported in its ledger entry
(`usage`): Claude's JSON result gives input, output and cache tokens and a
cost; the Codex CLI prints a total after "tokens used". `report` adds them up
per kind of call and per judge ("Paid calls"); a call whose tool printed no
count is counted as a call without usage, so a total is then a lower bound.

Generations use the local `codex` login; judges and reflection use
the local `claude` and `codex` logins. No API keys are read or passed: child
processes get the application's environment allowlist.

## The decision rule

Fixed in advance (`stats.decide`). A decision is made only when the
comparison covers every held-out species of the panel, from the judges that
passed calibration (see Calibration); a dev-only or partial comparison reports
its numbers with `decision: null` and a `decision_note` saying why. The
candidate replaces the baseline only if, on the held-out species:

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
pairwise judge agreement, Fleiss' kappa and each judge's share of choices that
went to side A (`position_bias`; sides are random per pair and judge, so a
judge far from 0.5 is choosing by position).

The owner remains the final authority: a passing result is the case for a
prompt change, not the change itself. Adopting a winner means a normal pull
request that updates `PLATE_INSTRUCTIONS`, bumps `PROMPT_VERSION` and records
the new digest.

## Calibration

`calibrate` judges pairs whose better plate is known, each twice with every
judge seeing the sides it saw the first time swapped:

- **Anchors** (`--anchors DIR`): `DIR/<taxon_id>/<name>.png` files, each a
  plate known to be worse than that species' approved catalog plate (for
  example a first attempt the pipeline review rejected). The species may be
  any approved catalog species, on the panel or not, so pairs built from the
  owner's past decisions can be used; freeze an off-panel species' references
  first with `cache-references --species ID`. A judge is right when it prefers
  the approved plate on at least one axis and on neither prefers the worse one.
- **Planted defects**, made from each approved plate: the lower body and legs
  stretched within the bird's column (the margin lettering and the ruler are
  untouched), a duplicated region where the legs usually are, and a replaced
  Length line with wrong digits. A region that holds too little drawing is
  refused rather than planted, so a defect is never pasted paper on paper.
  The judge finds the defect only when, in the same verdict, it reports a
  matching category more often on the planted plate than on the untouched
  original (a complaint it makes about every plate does not count), one such
  defect's location or description names the altered region (legs, feet,
  lower body or tail for the two drawing defects, the Length line for the
  lettering one), and the planted plate loses the comparison on the axis the
  defect damages (anatomy, or lettering).
- **Planted preference**: the judge must also prefer the untouched plate.
- **Swap consistency**: both orders must get the same answers.

Thresholds are in the configuration (0.8 each by default). Without anchors a
judge cannot pass. `calibration/summary.json` records the rubric digest, the
judging prompt and schema digest, the thresholds, each judge's kind and model,
its scores and its share of side A choices.

`compare` and `tune` read that record (or the one given with
`--calibration FILE`) before deciding anything. Only judges whose name, kind
and model match the record and who passed count; a change to the rubric, the
judging prompt or a threshold makes the record stale for everyone. A decision
needs at least `min_calibrated_judges` (2) such judges and, with
`require_each_provider` (on by default), at least one of each configured kind,
so a Claude-only majority cannot decide when a Codex judge is configured.
Without that, `compare` still generates, judges and reports every number but
writes `decision: null` with the reason; `tune` refuses to start.
`--allow-uncalibrated` lets both run for exploration only: the
output says `exploratory: true`, `decision` stays null and what the rule would
have said is under `exploratory_decision`. Judges that failed are still asked
in `compare` (their numbers are reported) but never decide; `tune` asks only
the calibrated judges.

Each verdict also records the rubric's digest, the judging contract's digest
and the judge's kind and model; after a change to any of them, the next
command judges those pairs again (and rebuilds the blind folders with the new
rubric) instead of reusing old verdicts.

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

The first `tune` in a run directory records what its scores mean in
`tune/evaluation.json`: the rubric and judging-contract digests, the judges
that score (name, kind and model), the score weights, the generation model and
attempts, the candidate length cap, the panel's dev and held-out species and
their facts, and the held-out samples. A later `tune` (its dry run included)
under any other value is refused with "use a new run directory", so saved
scores, GEPA's state and the selection are never mixed across objectives. A
run directory tuned before this record existed adopts the current
configuration.

## Not included

Tuning the review instructions is not part of this tool yet.

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

A judge's `model` defaults to `claude-fable-5-2` for `claude` and
`gpt-6-astra` for `codex`.

### Adding a judge

Add a `[[judges]]` entry with a unique name, a `kind` of `claude` or `codex`
and optionally a model, then run `judge-smoke` and `calibrate` again. A new
kind of judge is a class with a `name` and `judge(request) -> BlindVerdict`
(see `judges.py`) that reads only `request.blind_dir`, plus a branch in
`judges.build_judges`.

### The command lines

They are built in `judges.claude_command`, `judges.codex_command` and
`tune.ClaudeReflector.command`. Every flag was checked against the help of
Claude Code 2.1.309 (`claude -p --help`) and codex-cli 0.157.1
(`codex exec --help`; `gpt-6-astra` is in `codex debug models` with image
input). Help text cannot confirm these, so run `judge-smoke` (three real
calls) before a large run:

- how `--restricted`, `--safe-mode`, `--permission-prompts none` and
  `--allowedTools "Read(//<folder>/**)"` combine (the `//` absolute-path rule
  syntax is not in the help), and that `Read` inside the folder is allowed;
- that the JSON result carries `structured_output`, `usage` and
  `total_cost_usd` (the parser falls back to `result` text for the verdict);
- the Codex `-c` keys `web_search`, `mcp_servers` and `project_doc_max_bytes`,
  which `codex exec --help` does not list;
- that the Codex CLI prints its total after "tokens used" (otherwise usage is
  recorded as missing, not as zero);
- the model name `claude-fable-5-2`.

`judge-smoke` sends one approved plate against its stretched copy to each
configured judge (or each `--judge NAME`), and prints each parsed verdict,
which plate it preferred, the seconds it took, its token usage and its log
path under `smoke/`. Its verdicts never count anywhere.

### What a judge can see

Each judge has its own blind folder per pair, with its own random side
assignment. The plates in it are re-encoded from bare pixels: only the PNG
`IHDR`, `IDAT` and `IEND` chunks remain, so no ICC profile, text, resolution
or EXIF made by either arm's pipeline reaches the judge, and every entry,
the folder included, carries one fixed file time. Each judge call gets a fresh
copy of its folder (the two plates, the references and the rubric, all with
one fixed file time) in a new folder under the system
temporary folder, which is removed after the verdict. The folder is outside the
run directory and outside any repository (a temporary folder inside a project
is refused), so no key, other judge's verdict or project guidance is reachable
by a relative path. Codex's schema and answer files sit in a second temporary
folder.

- Claude judges may use only `Read`, scoped to that folder (`--tools Read`,
  `--restricted`), with no MCP servers, settings files, CLAUDE.md, skills,
  hooks or plugins (`--strict-mcp-config`, `--safe-mode`), and anything that
  would ask for a permission is refused (`--permission-prompts none`).
- Codex judges run in the `read-only` sandbox without the user's
  `config.toml` or execpolicy rules (`--ignore-user-config`, `--ignore-rules`),
  with web search, MCP servers and project guidance turned off. That sandbox still lets the agent read any file
  your account can read by its absolute path, and Codex still loads your global
  `AGENTS.md` under its home folder if you have one. A judge is never told
  where the run directory is, but nothing stops a judge that searched the disk
  for it; keep run directories out of the temporary folder.

## The run directory

```
run.json                  seed (blind sides, generation order and pair ids derive from it)
references/<taxon>/       frozen photos and their manifest
arms/<arm>.json           each arm's exact wording (or release commit) and digest
arms/<arm>.prompts.py     a release arm's prompt module, as git showed it
<arm>/<taxon>/<k>/        prompt.txt, codex.log, generated.png, portrait.png, sample.json
blind/<folder>/           what one judge sees of one pair: plate-A.png, plate-B.png, references/, RUBRIC.md
unblinding/<key>.json     which plate is A for each judge; its name is unrelated to the folders
verdicts/<pair>/<judge>.json   stamped with the rubric and judging-prompt digests and judge model; usage
compare/<first>-vs-<second>/summary.json and pairs.json (unblinded, run-relative paths)
.lock                     held by the command using the run directory
smoke/                    judge-smoke logs and summary
tune/evaluation.json      the tuning configuration this run's scores and GEPA state belong to
tune/selection.json       the candidate chosen for the held-out test
calibration/, tune/, gepa/, report/
```

A sample is finished only when `sample.json` is written, after the portrait.
It records the `generation_model`; a sample made with another model is refused
rather than reused, so use a new run directory after changing it.
A plate is prepared again without paying only when the ledger shows its
generation finished; a plate from a failed, timed-out or possibly unfinished
generation is deleted and generated again, so it is never judged. If the
harness itself is killed outright (not stopped with Ctrl-C or a normal
terminate), an agent it started can keep running: stop it before resuming.
Blind folders and frozen references are built in a hidden staging folder and
published by one rename.
