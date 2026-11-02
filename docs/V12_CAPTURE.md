# v12 capture plans

The coordinator uses only a saved coastline capture plan. By default it loads
`capture-plans/coastal-connection/plan.json` relative to the script, regardless
of the current working directory. `--plan` selects a different saved plan.
There is no centerline/corridor fallback or separate Y/Z prompt.
The v12 geometry is a local copy of the accepted
[`coastline_route_v12.py`](../capture-plans/coastal-connection/coastline_route_v12.py),
stored with the plan it defines. This is planning input, rather than a
temporary utility program.
Planning and auditing use only the Python standard library and issue no server
commands. The coordinator executes captures from saved plans.

`plan.json` is generated output, rather than a file users need to author by
hand. The generator is [`v12_capture_plan.py`](../v12_capture_plan.py). Its
small inputs are the shoreline definitions, Y bounds, section-size options,
and optional height/lake supplements. It expands those inputs into the
requested regions, export cuboids, names, embedded geometry, and hashes needed
for repeatable collection and coverage auditing.

For instructions, see [Generate or revise a plan](#generate-or-revise-a-plan).

## Isolated captures and compatible resume

**New collections use operational model `fresh-loaded-whole-section-with-overlaps/1`
and run schema `coast-capture-run/3`.** Each run freezes the plan and capture
settings, records unique raw export attempts, and verifies loading, column
coverage, and agreement between overlapping selections. Only current-format
run manifests can be resumed or audited.

Start the first run using a directory that does not yet exist or is completely empty:

```sh
python3 -B /path/to/vintage_story_export_coordinator.py \
  --plan /path/to/capture-plans/coastal-connection/plan.json \
  --run-dir "$HOME/vs-capture/coastal-connection-clean" \
  --target-pane PANE \
  --export-dir /path/to/server/data/WorldEdit \
  --server-log /path/to/server/data/Logs/server-main.log
```

`--help` lists all options, defaults, requirements, and combinations. Collection
is the default operation, and `--target-pane` is required only when collecting.
For a new run, omitted `--player` prompts with `Xplosionist` as the default;
omitted `--export-dir` prompts with `~/WorldEdit`. Omitted `--server-log` uses
`../Logs/server-main.log` relative to the export directory. The export directory
must exist and the log must be readable when creating or collecting a run.
Continuation uses the saved source values unless explicit values are supplied
to check against them.

`--run-dir` and `--resume` are mutually exclusive. Choose at most one of
`--preview`, `--prepare-only`, and `--report-coverage`. `--new-run` cannot be
combined with `--resume`, `--preview`, or `--report-coverage`; it can be combined
with `--prepare-only` and a new `--run-dir`. Plain `--preview` writes the source
plan's SVG without needing a run, player, export directory, log, or pane.
`--prepare-only` needs source inputs for a new run but sends no commands.
`--report-coverage` needs an existing compatible run and its export files, but
no active console, player, or server log.

New-run policy defaults are `--overlap 3`, `--coverage-y 109`, `--max-attempts 3`,
and `--rescan-y-min 0`. Overlap must be 1–16, the attempt limit must be positive,
and coverage/probe Y values must be nonnegative integers. Required confirmation
height is clamped to each selection's Y range and does not change its requested
export bounds. The program rejects invalid numeric values before prompting or
creating a run. `--step-by-step` prompts before primary captures and retries;
loading and any deeper probes remain automatic.

Repeat that command to continue after an interruption. You can also omit
`--run-dir` on restart: the coordinator remembers custom run directories and
automatically continues the one compatible run matching the plan and supplied
server/player/settings. Repeating a nonempty `--run-dir` continues that run
after compatibility checks. An existing empty directory starts a new run;
hidden files and subdirectories count as contents, so even those entries
prevent new-run initialization. A completed run remains reusable too.

Add `--prepare-only` to create the run, freeze its plan, and render its capture
preview without sending commands. After reviewing the preview, repeat the
command without `--prepare-only`, or explicitly select the prepared run below.
If no prior run matches and `--run-dir` is omitted, a new writable directory is
created under `./capture-runs/RUN_ID`. The installed program and source plan
need not be writable. `-B` suppresses Python bytecode-cache writes.

```sh
python3 -B /path/to/vintage_story_export_coordinator.py \
  --resume "$HOME/vs-capture/coastal-connection-clean" --target-pane PANE

python3 -B /path/to/vintage_story_export_coordinator.py \
  --resume "$HOME/vs-capture/coastal-connection-clean" --report-coverage
```

The run retains the player, actual export directory, and server-log path.
`--tmux-binary /path/to/tmux` can be supplied on either collection command.
Use `--verbose` for one progress line per completed deeper probe, saved-export
filenames during reuse checks, primary/probe counts in completion summaries,
successful overlap comparisons, and every final-audit selection. Use
`--step-by-step` to confirm primary captures.
A report needs no pane and sends no commands. `--plan PLAN
--report-coverage` also discovers and audits a compatible run automatically.
If none is found, reporting stops with an error; loose export files are never
adopted by filename. Plan-only `--preview` renders the planned cores; use
`--prepare-only` or `--resume RUN_DIRECTORY --preview` for an isolated run's
padded mosaic.

Use `--new-run` to deliberately start a separate capture of the same plan.
It bypasses automatic continuation and requires a nonexistent or completely
empty directory if `--run-dir` is supplied. Prior runs and their exports remain
untouched. If several runs
match, the coordinator asks which to continue rather than guessing. Supplying
`--export-dir`, `--server-log`, or `--player` narrows the search. If a matching
run is incompatible or unreadable and no compatible run exists, the coordinator
shows the problem and asks whether to stop for review or create a separate run.
Reporting stops on incompatibility because it cannot create a new run.

Run locations are indexed in
`$XDG_STATE_HOME/vintage-story-export-coordinator/runs.json`, defaulting to
`~/.local/state/vintage-story-export-coordinator/runs.json`. Set
`VS_CAPTURE_RUN_INDEX` to choose another index file, for example in a writable
working directory. The index stores locations and identity metadata; each run
manifest remains authoritative. Discovery also checks manifests in the current
directory, beside the plan, and their immediate child/run directories. For a
current-format run elsewhere that is not indexed, use `--resume DIRECTORY`
once to register it.

Each run gets a UTC timestamp plus random ID. Its `run.json`, frozen `plan.json`,
`RUN_ID-progress.json`, `RUN_ID-preview.svg`, `RUN_ID-audit.json`, and `commands/`
are kept together. The plan/geometry hashes are recorded separately from the
run ID. Export names include that ID, selection ID, purpose, and a new random
attempt token. `--export-dir` must identify where the server actually writes
WorldEdit exports; the flag does not redirect the server's own output.

For downstream analysis, `sections[UNIT_ID].selected.file` in the progress
manifest is the authoritative primary file for that selection. Join it to
`units[].core` and `units[].capture` by unit ID. Crop primary records to the
nonoverlapping core when assembling a datamap; the raw file also contains its
intentional overlap strips. Deep probes remain supplemental evidence. Never
glob all JSONs or combine superseded attempts as if they were additional
terrain: the manifest deliberately retains failed and replaced files too.
An interrupted, not-yet-certified primary appears under `candidate` until its
column confirmations finish. Reports retain that pending candidate's file
references but do not credit the whole selection prematurely.

Compatible full or partial runs are reusable automatically or through explicit
`--resume`.
Each run permits only one controller/report writer at a time via an advisory
lock; do not run multiple controllers against the same server/player either.
Only files selected by that run's manifest are eligible. Resume verifies the
frozen plan, operational model, immutable source/settings, exact bounds,
timestamps, file hashes, loading evidence, and deeper confirmations. An
unrelated same-sized export is never discovered and adopted automatically.
Finished data stays reusable. A section interrupted during deeper confirmation
can continue its immutable primary candidate and already verified probes from
that same attempt. Each completed probe is checkpointed. A contradictory or
invalid attempt is rejected as a whole and its replacement gets a new primary
filename; the rejected files remain as provenance. There is no need to empty
the server's WorldEdit directory or remove unrelated exports.

Changing status wording or diagnostics does not inherently change the model.
Changes to loading, freshness, replacement, coverage, or comparison semantics
must change the operational-model identifier. An old/unknown model or a changed
`GameVersion` stops for compatibility review rather than silently reusing or
deleting data. Continuation assumes the same saved-world baseline; if a different
save is loaded at the same filesystem path, use `--new-run`. The controller
cannot infer that world-baseline change from filenames alone.

The default whole-section retry budget is three attempts. Explicitly raising
it with `--max-attempts N` on resume preserves compatible evidence; changing
the retry budget does not relax the validation rules. Changes to `--overlap`,
`--coverage-y`, or `--rescan-y-min` require a new run definition.

### Investigation after automatic retries

After the automatic attempt budget is exhausted, an interactive collection
saves its checkpoint and pauses. It prints the affected selection's actual
export bounds, attempts used, last failure reason, and progress path. While
paused, it sends no server commands, so you may fly around to load chunks or
investigate the server before deciding what to do.

The recovery prompt offers:

- `r`: authorize one additional fresh whole-selection attempt. It performs
  force-loading, teleporting, chunk-queue checks, and normal validation again.
- `w`: extend the investigation time by restarting the response timer.
- `q`: save and quit with the selection incomplete.

The default response timeout is **300 seconds**. Set `--recovery-timeout SECONDS`
to change it, or `--recovery-timeout 0` to wait indefinitely. Invalid input does
not extend the timer. Timeout, end of input, or Ctrl-C at this prompt stops
with progress saved. Without interactive terminal input, collection stops
immediately instead of waiting. Stopping on an unresolved selection returns
exit status 1. Restarting the same command brings that selection back to the
recovery prompt if its budget is still exhausted; a higher `--max-attempts`
can instead authorize more automatic attempts.

Manual retry permission applies only to the affected selection, is recorded
in its checkpoint, and survives a restart. It does not reset attempt history,
increase other selections' budgets, or accept a failed export. If that one
extra attempt fails, the program returns to the recovery prompt. There is no
split, skip, or manual-accept choice in this menu. Compatibility errors and
failures to save checkpoints still stop collection for review.

### Loading, fresh files, and whole-section replacement

Every export, including the first section and every probe, requests force-load
for its X/Z area, teleports the player, and checks both player-based chunk
queues before selecting and exporting. The local server log is required for
command acknowledgements. Failed loading or exports cannot be accepted merely
because a nonzero block count was logged.

Each command requires a previously absent output name. Existing names are
refused, never overwritten or archived automatically. The new file must be a
regular file with modification/change timestamps after command issuance and
must stop changing. The run records the exact command time, timestamps, size,
SHA256, and load responses. Clock/filesystem timestamp inconsistencies fail
the attempt rather than weakening freshness checks.

The primary export must account for every captured X/Z column through Y=109
by default (`--coverage-y` changes this for a new run). It checks a record at
the required height and record presence in every intersecting 32-block
vertical stratum below it. Missing evidence triggers targeted 32-by-32 probes
from Y=0 through the selection ceiling. A probe must contain records in every
column; for each affected column, its entire in-band block/decor multiset must
match the primary export. This corroborates genuine shafts/empty intervals
without inserting artificial air records.

If a probe instead recovers different records, the **entire primary selection**
is retried under a new name. Present records from its older attempt are not
retained. The chosen main export is always one untouched raw WorldEdit file;
there are no synthesized, column-stitched section files. All superseded
attempts and their hashes remain in the run history. Probes are bound to the
same section attempt and are not borrowed from a different run or attempt.

### Overlap checks and completion gates

Plan cores stay nonoverlapping. Raw selections extend three blocks east and
south by default, producing shared three-row/column strips and corner overlaps.
All intersecting selections are compared, including side-by-side X pieces.
The planner subdivides cores first if padding would violate the 1023 X/Z/Y
packed-index limits or the selection-volume cap. The current 160-unit plan
still uses 160 selections, with at most 453 X by 116 Y by 53 Z blocks each.

For every shared position, comparison uses **block-code multisets**, preserving
duplicate solid/fluid entries rather than comparing palette IDs or collapsing
indices. Decor comparisons include block codes and face/subposition. The
[official schematic format](https://github.com/anegostudios/vsapi/blob/master/Common/Collectible/Block/BlockSchematic.cs)
stores decor block IDs in the low 24 bits. Entities/block-entity payloads are
retained in raw files but are not part of the terrain seam comparison.

A mismatch blocks completion and requests a fresh whole export of an affected
section. Neighbor checks are repeated against its replacement. Persistent
differences reach the recovery prompt at the retry limit with `finished: false`;
timeout or noninteractive operation stops with progress saved. The final audit
rechecks the selected files and probes and recomputes overlap fingerprints.
It records every overlap's bounds, source filenames/hashes, and comparison
digests; mismatches include differing-position counts and sample multisets.
Exact requested coverage gaps are listed separately.

In schema 3, `collection_finished` means all selections have validated raw
captures; **`finished` requires required-height coverage, all overlap checks,
all exact file hashes, and a completed final audit**. `coverage_complete` and
`column_coverage_complete` describe that required-height evidence.
`full_3d_coverage_complete` and `full_3d_gaps` retain the conservative result for
the entire original Y range, including unverified omitted upper air. Thus a
certified run can finish while formal height gaps above the required surface
remain. Requested upper-ceiling block/decor hits remain visible as height warnings;
review those before deciding that the chosen Y maximum is sufficient.
Evidence is still conditional on the exporter returning loaded contents;
overlap agreement is not proof of a simultaneous snapshot or of unchanged
interiors between export times.

## Prepared plans: Y=65–180

| Plan | Export units | Requested block positions | Extra exported positions |
| --- | ---: | ---: | ---: |
| Current: X ≤450, Z ≤50 | 160 | 311,148,656 | 8,473,916 (2.72%) |
| Previous: X ≤1023, Z ≤50 | 88 | 311,148,656 | 8,473,916 (2.72%) |

The current plan is in `capture-plans/coastal-connection/plan.json`.
Its units are at most **450 × 116 × 50** (X × Y × Z), or **2,610,000**
block positions. The first unit is 446 × 116 × 25. This smaller operational
width aims to reduce the area that must load around each player teleport;
it is not a measured guarantee of server chunk residency.

The earlier 88-unit revision is preserved in Git history at the same path.
[The plan changelog](../capture-plans/coastal-connection/CHANGELOG.md) keeps a
readable description of each revision beside the current plan. There are no
numbered or hash-named revision directories. The directory also contains a
generated `plan.svg` preview and a local PNG rendition. These previews are
ignored by Git; the SVG can be regenerated with `--preview`. Broader bands reduce command and
file-wait overhead by including edge terrain outside the exact footprint. Its
cuboids remain nonoverlapping and its audit uses the exact requested footprint.
No freshwater body definitions
have been supplied, so these prepared plans include no whole-lake supplements.

## Preview, run, resume, and audit

Use absolute paths when running from another directory. Relative command-line
paths resolve from cwd; the default plan resolves beside the installed script.

```sh
python3 -B /path/to/vintage_story_export_coordinator.py --plan PLAN.json --preview
python3 -B /path/to/vintage_story_export_coordinator.py --plan PLAN.json \
  --target-pane PANE --export-dir EXPORT_DIRECTORY --server-log SERVER_LOG
python3 -B /path/to/vintage_story_export_coordinator.py --plan PLAN.json --report-coverage
```

The execution command prompts for the player name when creating the run.
Repeat it to continue compatible work. The saved run retains the player,
export directory, server log, and settings. Changing those inputs requires a
separate run; changing the tmux pane is permitted. Reports need a matching run
manifest and send no server commands. `--resume RUN_DIRECTORY` explicitly
selects a run for collection, preview, or audit.

The startup banner identifies the run, progress file, and preview. A fresh run
says there are no saved exports to revalidate and describes the capture steps.
Only runs with saved primary exports or interrupted candidates announce checks
for reuse. Before reusing a selected export, collection revalidates its exact
file hash, bounds, loading evidence, column coverage, and deeper confirmations.
Each saved selection gets a validation message before its files are read and
a result afterward. Large exports can take time to parse; the status signal
identifies the exact file being checked.

Normal capture output is limited to useful milestones: one set of actual
selection bounds and dimensions (with an overlap note), any deeper-confirmation
plan, and a saved result with elapsed time. Before the first export starts, a
short "Exporting primary selection ..." line announces the work, including
chunk loading and teleporting. Its result follows after processing: either the
deeper-confirmation plan or the saved completion result. Attempt numbers and
reload details appear only when another attempt becomes necessary. There
are no separate success messages for chunk queues, file stability, hashing,
each validation phase, original core bounds, or newly generated export paths.
Those detailed phases still update the immediate status signal; filenames and
bounds also remain in the saved run records and command files.

When deeper evidence is needed, one line gives the column count, planned probe
count, loading/teleport/export actions, and Y range. Normal output samples probe
progress after about 15 seconds between completed probes. `--verbose` prints
exactly one short result for every successful completed probe. For three probes,
a successful verbose selection normally occupies seven lines, plus at most one
useful overlap summary. Verbose completion summaries retain the primary-export
and deeper-probe counts; normal summaries use the shorter validated/saved result.
Only `--verbose` reports successful overlap comparisons, explaining agreement
of shared block records with neighboring exports and which remaining checks
need neighboring captures. There is no summary if no neighboring export could
actually be compared. Overlap mismatches remain visible in both modes.
Failures, retries, prolonged chunk-queue waits, and recovery prompts remain
visible in either mode. Captured selections still require overlap checks and
the final audit before the run is certified complete. These reporting choices
do not change run compatibility or capture policy.

The collector has no `--prefix` or `--z-span` option. Change geometry, height,
and section sizes with `v12_capture_plan.py`, review the resulting plan, then
start a new capture run. Missing or invalid plans stop execution.

`tmux` is found in `PATH`, or selected with `--tmux-binary /absolute/path/to/tmux`.
The chosen executable is used for every command, including loading and probes.
A writable run directory, export directory, server log, and run index are
needed; the installed script and source plan need not be writable. Use `-B`
to avoid bytecode-cache writes beside installed Python modules.

Reports write `RUN_ID-audit.json` inside the run directory. The report contains
all core/capture bounds, selected primary/probe filenames and hashes, exact
required-height and full-height gaps, overlap checks, file errors, and ceiling
warnings. The progress checkpoint retains pending candidates and superseded
attempts. See `sections[UNIT_ID].status`, `selected`, `candidate`, and `error`
for each selection's current state. Exit codes are 0 for a certified complete
run, 1 for an incomplete run, 2 for invalid inputs, and 130 for an interruption.

### Immediate status: Ctrl-T and signals

On macOS/BSD, press **Ctrl-T** while the coordinator is running to request an
immediate status report via `SIGINFO`. The terminal's status character must
be enabled; if remapped, `stty status '^T'` restores it on those systems.

On Linux, use **`kill -USR1 PID`** from another shell. Linux does not support
the terminal `SIGINFO`/Ctrl-T mechanism. The operating system running Python
determines this, so a Mac terminal connected to a Linux server still uses the
Linux method. The coordinator prints its PID and the status command at startup.
`SIGUSR1` also works on macOS/BSD, and `kill -INFO PID` works where `SIGINFO`
is available. See the [terminal status-character documentation](https://man7.org/linux/man-pages/man3/termios.3.html).

The status report includes captured/verified selection counts, incomplete,
waiting and pending counts, elapsed time, current operation and its duration,
selection bounds, and saved deeper confirmations for the current attempt. It
also shows the final coverage/overlap/audit gates separately from captured
selection counts. Status is printed to stderr without pausing collection,
sending commands, writing checkpoints, or starting a fresh audit.
Ctrl-C saves a checkpoint and offers retry or quit; a second Ctrl-C aborts.

## Geometry and completeness

For integer Z=59375–59749 the west boundary is X=-34500 and the east boundary
is the east shore. For Z=59750–61994 the boundaries are the two shores. For
Z=61995–62360 the west boundary is the west shore and the east boundary is
X=-32680. These anchors describe capture coverage, not cut/fill limits.

Each west coordinate is rounded down and each east coordinate up. Cubic
Bezier intersections are enclosed using rational bisection and an exact
derivative bound rather than approximate polylines. Geometry that is not
Z-monotone is rejected. The tiny numerical enclosure can conservatively include
an additional boundary block if it straddles an integer; it cannot omit a
shoreline block. The integer footprint is widened by 100 X blocks and then
unioned over every source row within 100 Z blocks, including outside the
original Z range. The resulting Z extent is 59275–62460.

The planner normalizes the union into disjoint height slabs and row intervals,
then partitions export units. X splitting happens during planning. New plans
default to initial X widths of 450 and Z lengths of 50, saved independently
of the hard 1023-block side limit and 9,000,000-block selection-volume limit.
The run planner adds overlap padding and subdivides cores further if needed
to stay within the same side and volume limits.

Planning verifies that cores do not overlap and requested minus planned
coverage is empty. Cuboids may include extra edge terrain beyond the minimum
100-block horizontal margin. Run auditing computes exact required regions
minus validated core coverage; overlaps cannot inflate coverage. It also
reports conservative gaps over the full initial Y range. A shortened SizeY
does not silently establish omitted upper space as air.

Raw file validation records every X/Z column with no block/decor entry.
Collection additionally checks the required surface height and vertical chunk
strata, using fresh deeper probes when evidence is missing. Those probes must
agree with all in-band records of the selected primary; otherwise the whole
selection is retried. Sparse JSON alone cannot establish chunk residency or
prove a simultaneous world snapshot. Loading acknowledgements, column
confirmation, file provenance, and overlap comparisons supply the run's stated
evidence, with these limits recorded in its audit.


## Generate or revise a plan

Run the examples from the repository root. A Python interpreter is sufficient;
there are no third-party Python dependencies. The generator does not use tmux,
contact the game, or send server commands.

### Reproduce the current plan

This is the tool and configuration used to generate the current 160-section
plan. A fresh temporary output directory lets you inspect the result without
changing the maintained plan:

```sh
plan_draft_dir=$(mktemp -d)
python3 v12_capture_plan.py create \
  --geometry capture-plans/coastal-connection/coastline_route_v12.py \
  --ymin 65 --ymax 180 \
  --x-span 450 --z-span 50 \
  --output "$plan_draft_dir/plan.json"
```

The command prints its output locations and writes `plan.json` and `plan.svg`
in that temporary directory. With the current source and options, its content
hash matches the committed 160-section plan. You can compare the complete JSON:

```sh
cmp "$plan_draft_dir/plan.json" capture-plans/coastal-connection/plan.json
```

`cmp` prints nothing and exits successfully when the files match.

### Choose the change you need

| Desired change | Inputs and command |
| --- | --- |
| Change the requested height for the entire footprint | `create --ymin MIN --ymax MAX` |
| Use smaller or larger initial sections | `create --x-span WIDTH --z-span DEPTH` |
| Change the shoreline shape within the existing v12 area | Edit a geometry-file copy and use `create --geometry FILE` |
| Add height over selected regions in a revised plan | `extend --plan FILE --regions REGIONS.json` |
| Include whole intersecting freshwater bodies | `create --freshwater BODIES.json`, or `extend --plan FILE --freshwater BODIES.json` |

Every `create` command also requires `--ymin` and `--ymax`. Use `--output` to
write a draft. `--x-span` defaults to 450 and `--z-span` to 50; these are maximum
sizes, so edge sections can be smaller. Both must be integers from 1 to 1023,
and the generator also enforces the selection-volume limit. Y bounds are
inclusive: 65 through 180 is 116 blocks high.

For example, generate smaller sections without changing the requested height:

```sh
plan_draft_dir=$(mktemp -d)
python3 v12_capture_plan.py create \
  --ymin 65 --ymax 180 \
  --x-span 256 --z-span 25 \
  --output "$plan_draft_dir/plan.json"
```

The geometry file defaults to the v12 shoreline source beside the maintained
plan. Without `--output`, the destination is the maintained
`capture-plans/coastal-connection/plan.json`. Both defaults are relative to the
generator's location, regardless of cwd. Explicit relative paths are relative
to cwd; use absolute paths when invoking the program from another directory.
The generator allows identical regeneration but refuses to overwrite an
existing plan with different contents. Use a fresh draft path for each variant.

### Edit the shoreline geometry

Start with a copy of the human-maintained source, not the generated JSON:

```sh
geometry_draft_dir=$(mktemp -d)
cp capture-plans/coastal-connection/coastline_route_v12.py \
  "$geometry_draft_dir/coastline.py"
```

Edit `coastline.py` in that directory. The two literal lists `WEST_SHORE` and
`EAST_SHORE` describe paths in relative world X/Z block coordinates:

| Operation | Format and meaning |
| --- | --- |
| `M` | One `(X, Z)` point: the start of the shore. Each path starts with one `M`. |
| `L` | One `(X, Z)` point: a straight segment from the preceding endpoint. |
| `C` | Three `(X, Z)` points: two control points and the endpoint of a cubic Bezier curve, starting from the preceding endpoint. |

For example, this existing west-shore segment has two control points followed
by its endpoint:

```python
('C', [(-33728, 61470), (-33678, 61503), (-33665, 61525)])
```

Use numeric literals, including decimals where needed. The loader reads these
definitions as data and does not execute the Python file or evaluate imports,
functions, or calculated expressions.

Each segment must move forward in Z. Its control points must have
nondecreasing Z values, and its endpoint must have a greater Z than its start.
The west shore must cover Z=59750–62360 and the east shore Z=59375–61994. The
generator rejects unsupported paths, missing row coverage, and reversed
west/east capture boundaries. Regenerate after editing:

```sh
plan_draft_dir=$(mktemp -d)
python3 v12_capture_plan.py create \
  --geometry "$geometry_draft_dir/coastline.py" \
  --ymin 65 --ymax 180 \
  --x-span 450 --z-span 50 \
  --output "$plan_draft_dir/plan.json"
```

This generator is currently specific to the v12 coastal connection. The Z
bands, mouth anchors, 100-block buffer, coordinate offset, and v12 geometry
label are fixed in the program; they have no CLI options. `--geometry` permits
shore-shape revisions within those constraints. Planning a completely
different world region or changing those rules requires modifying the planner.

### Review and use a draft

Open the emitted `plan.svg` in an image viewer or browser. It shows the
required footprint and export mosaic. Generation also checks that export
units do not overlap, obey size/volume limits, and cover every requested region.
The printed summary gives section counts, maximum unit volume, and extra
captured volume.

You can explicitly preview the draft with the coordinator, without sending
commands or changing the maintained plan:

```sh
python3 vintage_story_export_coordinator.py \
  --plan "$plan_draft_dir/plan.json" --preview
```

For a deliberate server test of that draft, choose the pane and actual WorldEdit
export directory, then run:

```sh
python3 vintage_story_export_coordinator.py \
  --plan "$plan_draft_dir/plan.json" \
  --target-pane PANE --export-dir EXPORT_DIRECTORY \
  --step-by-step --verbose
```

That last command sends server commands after your confirmation. Progress and
reports go in the run directory; raw world exports go in the configured server
export directory. The run freezes its own plan copy, so later changes to the
draft do not alter an existing capture.

### Make a reviewed revision the maintained plan

Existing runs already contain frozen plan copies. Promote the reviewed JSON:

```sh
cp "$plan_draft_dir/plan.json" capture-plans/coastal-connection/plan.json
python3 vintage_story_export_coordinator.py --preview
```

If you edited the shoreline copy in the geometry example, also promote that
reviewed source so future generation uses the same geometry:

```sh
cp "$geometry_draft_dir/coastline.py" \
  capture-plans/coastal-connection/coastline_route_v12.py
```

Commit changed geometry or supplement input files along with the plan so the
result can be reproduced. Update the adjacent
`CHANGELOG.md` with what changed and why, then commit it together with
`plan.json`. Earlier full plan contents belong in Git history:

```sh
git log --oneline -- capture-plans/coastal-connection/plan.json
git show COMMIT:capture-plans/coastal-connection/plan.json
```

The preview command regenerates SVG only; a local PNG snapshot is not updated
automatically. Regenerate or discard an outdated PNG after changing the plan.

Do not hand-edit `requests`, `cuboids`, export names, or hash fields in the
expanded JSON. Editing it invalidates its content hash; changing the input and
regenerating keeps geometry, coverage, and identifiers consistent. `create`
assigns a new plan identity when inputs change. `extend` adds height/lake
coverage while preserving existing partition bounds; the revised plan requires
a separate capture run. A plan file contains geometry, not collected terrain.

## Additional height coverage

Blocks/decor at a requested upper ceiling are flagged during execution and in
the audit. A ceiling hit is a warning that the chosen height may truncate the
terrain; it does not change the requested bounds or prove that more height is
unnecessary. Choose additional bounds explicitly after inspection.

Supply a JSON file such as `height-regions.json`:

```json
{"regions": [{"xmin": -33600, "xmax": -33000, "ymin": 181, "ymax": 220,
              "zmin": 61500, "zmax": 62000}]}
```

```sh
python3 v12_capture_plan.py extend --plan PLAN.json --regions height-regions.json --output /tmp/coastal-connection-revised.json
```

This creates a draft revision with a separate plan identity and export-unit
names. It adds only regions outside the existing planned units and deduplicates
overlapping requests. The new units inherit the parent plan's explicit X/Z
caps. `requested_y` records the initial bounds; `added_regions` and `requests`
record all additional bounds. Start a separate capture run for the revised
plan; exports from the original plan are not adopted into it.

## Whole freshwater bodies

Create a JSON file with complete body footprints as integer row intervals:

```json
{"bodies": [
  {"id": "lake-north", "rows": [[60000, -34200, -34000],
                                  [60001, -34210, -33990]],
   "ymin": 65, "ymax": 180}
]}
```

List **every** row of each complete body, with multiple intervals on a row if
needed. The example illustrates the format, not an actual mapped lake. If any
body column intersects the buffered coastline footprint, the entire supplied
body is added to the requested union. Body-specific Y bounds are optional;
they default to the initial capture Y bounds. Nonintersecting bodies are
recorded but not selected. Overlapping bodies are unioned before partitioning.
No claim is made that the supplied body is complete or freshwater: those
attributes must come from mapping/world analysis outside this coastline planner.

```sh
python3 v12_capture_plan.py create --ymin 65 --ymax 180 --freshwater freshwater-bodies.json --output /tmp/coastal-connection-revised.json
python3 v12_capture_plan.py extend --plan PLAN.json --freshwater freshwater-bodies.json --output /tmp/coastal-connection-revised.json
```

The extension command adds newly mapped lakes in a draft plan revision while
preserving existing partition bounds and adding only body regions outside
previously planned units. The revised plan requires a separate capture run. Repeated body IDs must keep the
same definition; use a new ID for revised boundaries. Height extension is also
available for rectangular supplemental regions, but does not discover lake
boundaries.

## Validation

```sh
python3 -B -m unittest discover -s tests -v
```

Tests include exact subtraction against a voxel oracle, independent high
precision curve inversion, mouth/gap transitions and buffer ends, selection
limits, disjoint partitions, hash integrity, lake inclusion, height extensions,
raw export validation, whole-section retries, deeper shaft confirmations,
overlap mismatches, final completion gates, automatic compatible continuation,
and status signals. They use simulated exports and do not contact a server.

Application modules are at the repository root, instructions in `docs/`, and
tests in `tests/`. The plan JSON, coastline input, and visible changelog belong
together in `capture-plans/coastal-connection/`. Generated previews, commands,
progress, and audits are excluded from Git. Retain current run manifests and
selected exports when needed for continuation or audit.
